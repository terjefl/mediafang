"""Find new items for everything we follow, download them, and tidy up after sources that only
keep recent items."""

import logging
import random
import shutil
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, GeoRestrictedError, parse_bytes

from . import providers
from .config import Config
from .db import DB, Item, Source
from .library import (
    Target,
    needs_nfo,
    source_root,
    subtitle_name,
    target,
    write_episode_nfo,
    write_movie_nfo,
    write_show_nfo,
)
from .poster import make_poster
from .providers import youtube

log = logging.getLogger(__name__)

# yt-dlp's NRK messages for rights that are gone for good
_EXPIRED = ("har gått ut", "Ikke tilgjengelig", "ProgramRightsHasExpired", "NoProgramRights")
_GEO = ("utenfor Norge", "IsGeoBlocked")
# ...and for something not out yet ("NRK said: Kommer i dag kl. 21:11", or still on air)
_NOT_YET = ("NRK said: Kommer", "ProgramRightsAreNotReady", "ikke se eller høre programmet")
NOT_YET_WAIT = 30 * 60
# YouTube telling us to slow down: wait this long, doubling up to a day
THROTTLE_WAIT = 30 * 60
THROTTLE_WAIT_MAX = 24 * 3600


@dataclass
class RunResult:
    found: int = 0
    downloaded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    removed: int = 0
    errors: list[str] = field(default_factory=list)
    changed: set[Path] = field(default_factory=set)  # folders for Plex and Jellyfin to scan


@dataclass
class Progress:
    """What is downloading right now, for the web page."""

    label: str = ""
    source_id: int = 0
    percent: float = 0.0
    speed: str = ""
    started: float = 0.0


class _YtdlpLogger:
    def debug(self, msg: str) -> None:
        log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        log.warning(msg)

    def error(self, msg: str) -> None:
        log.error(msg)


class Harvester:
    def __init__(self, cfg: Config, db: DB, progress: Progress | None = None):
        self.cfg = cfg
        self.db = db
        self.tmp_dir = cfg.data_dir / "tmp"
        self.progress = progress or Progress()

    # ---- finding items -------------------------------------------------------

    def scan(self, source: Source) -> int:
        return providers.get(source.provider).scan(self.db, source, self.cfg)

    # ---- downloading ---------------------------------------------------------

    def _params(self, **extra) -> dict:
        return {
            "quiet": True,
            "noprogress": True,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
            "logger": _YtdlpLogger(),
            "retry_sleep_functions": {"http": _backoff, "fragment": _backoff},
            "ratelimit": parse_bytes(self.cfg.rate_limit) if self.cfg.rate_limit else None,
            "progress_hooks": [self._on_progress],
            **extra,
        }

    def _on_progress(self, d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            self.progress.percent = 100 * d.get("downloaded_bytes", 0) / total if total else 0
            self.progress.speed = (d.get("_speed_str") or "").strip()

    def download(self, item: Item) -> tuple[str, "list[Path] | youtube.Decision"]:
        """('done', files), or ('skip'|'wait', the decision) when the item itself says so."""
        source = self.db.source(item.source_id)
        provider = providers.get(source.provider)
        options = provider.ytdlp_options(self.cfg, source, item)
        postprocessors = [
            {"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"},
            *options.pop("postprocessors", []),
            {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True},
        ]
        with YoutubeDL(self._params(**options)) as ydl:
            info = ydl.extract_info(provider.video_url(item), download=False)
        if source.provider == "youtube":
            decision = youtube.prepare(source, item, info)
            if decision.action != "ok":
                return decision.action, decision

        t = target(self.cfg, source, item)
        t.folder.mkdir(parents=True, exist_ok=True)
        # Plex scans the moment the video lands, and its NFO agent identifies the item by the
        # metadata it finds then. So every sidecar must exist before the video does.
        written: list[Path] = []
        try:
            if source.layout == "movie":
                written.append(t.nfo)
                write_movie_nfo(t.nfo, item)
                self.ensure_art(source, t.root, item.image)
            elif needs_nfo(source):
                self.ensure_art(source, t.root, item.image)
                written.append(t.nfo)
                write_episode_nfo(t.nfo, source, item)
            written += self._thumbs(t, item.image or info.get("thumbnail"))
            # What ends up in the file's own tags (Infuse, VLC and friends read these)
            info.update(title=item.title, description=item.plot)
            if source.layout == "tv":
                info.update(series=source.folder_name if source.provider == "youtube" else source.title,
                            season_number=item.season, episode_number=item.episode)
            params = self._params(
                **options,
                paths={"home": str(t.folder), "temp": str(self.tmp_dir)},
                outtmpl={"default": f"{t.basename}.%(ext)s"},
                postprocessors=postprocessors,
            )
            with YoutubeDL(params) as ydl:
                result = ydl.process_ie_result(info, download=True)
            video = Path(result["requested_downloads"][0]["filepath"])
            written.append(video)
            written += self._rename_subtitles(t)
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return "done", [video, *[p for p in written if p.exists() and p != video]]

    def _rename_subtitles(self, t: Target) -> list[Path]:
        """yt-dlp names subtitles <base>.<track>.srt; give them names the servers understand."""
        renamed = []
        prefix = t.basename + "."
        for path in t.folder.iterdir():
            if not (path.name.startswith(prefix) and path.suffix in (".srt", ".vtt")):
                continue
            lang = path.name[len(prefix) : -len(path.suffix)]
            if "." in lang:  # already one of ours
                continue
            if path.suffix == ".vtt":  # conversion failed; keep the WebVTT, Jellyfin reads it
                new = path.with_name(subtitle_name(t.basename, lang)[: -len(".srt")] + ".vtt")
            else:
                new = path.with_name(subtitle_name(t.basename, lang))
            if new != path:
                path.replace(new)
            renamed.append(new)
        return renamed

    def _thumbs(self, t: Target, url: str | None) -> list[Path]:
        """The item's still: <base>.jpg for Plex, <base>-thumb.jpg for Jellyfin (and Pinchflat)."""
        if not url:
            return []
        plex_thumb = t.folder / f"{t.basename}.jpg"
        jellyfin_thumb = t.folder / f"{t.basename}-thumb.jpg"
        try:
            _fetch(url, plex_thumb)
            shutil.copyfile(plex_thumb, jellyfin_thumb)
        except Exception as e:  # cosmetic; never fail a download over it
            log.warning("Could not fetch the still for %s: %s", t.basename, e)
        return [plex_thumb, jellyfin_thumb]

    def ensure_art(self, source: Source, root: Path, fallback_image: str | None = None, force: bool = False) -> None:
        """tvshow.nfo, poster.jpg and fanart.jpg in a show's folder (a film's folder gets the
        pictures). NRK has 2:3 posters for some titles only; the others get one made from their
        16:9 picture. YouTube channels get their avatar and banner. Cosmetic: never fails."""
        try:
            root.mkdir(parents=True, exist_ok=True)
            if source.layout == "tv":
                write_show_nfo(root / "tvshow.nfo", source)
            wide = source.backdrop or source.image or fallback_image
            fanart = root / "fanart.jpg"
            if wide and (force or not fanart.exists()):
                _fetch(wide, fanart)
            poster = root / "poster.jpg"
            if force or not poster.exists():
                if source.poster:
                    _fetch(source.poster, poster)
                elif image := source.image or fallback_image:
                    self.tmp_dir.mkdir(parents=True, exist_ok=True)
                    src = self.tmp_dir / f"poster-src-{source.id}.jpg"
                    _fetch(image, src)
                    make_poster(src, source.title, providers.NAMES[source.provider], poster)
                    src.unlink(missing_ok=True)
        except Exception as e:
            log.warning("Could not update artwork for %s: %s", source.title, e)

    def refresh_art(self) -> int:
        """Rewrite every followed show's tvshow.nfo, poster and background."""
        n = 0
        for source in self.db.sources():
            if source.layout == "tv":
                self.ensure_art(source, source_root(self.cfg, source), force=True)
                n += 1
        return n

    # ---- clean-up ------------------------------------------------------------

    def apply_retention(self) -> tuple[int, list[Path]]:
        """Delete items older than their source's keep_days. Returns how many, and the folders
        that changed."""
        removed, changed = 0, []
        for source in self.db.sources():
            if not source.keep_days or not (source.dated or source.provider == "youtube"):
                continue
            cutoff = date.today() - timedelta(days=source.keep_days)
            for item in self.db.done_before(source.id, cutoff):
                t = target(self.cfg, source, item)
                for path in [Path(f) for f in item.files] + _sidecars(t):
                    path.unlink(missing_ok=True)
                if t.folder != t.root and t.folder.is_dir() and not any(t.folder.iterdir()):
                    t.folder.rmdir()
                self.db.set_status(item, "removed")
                removed += 1
                changed.append(t.root)
                log.info("%s: removed %s (older than %d days)", source.title, t.basename, source.keep_days)
        return removed, changed

    # ---- one run -------------------------------------------------------------

    def run(self, stop=None) -> RunResult:
        result = RunResult()
        for source in self.db.sources(enabled_only=True):
            try:
                result.found += self.scan(source)
            except Exception as e:
                missing = type(e).__name__ == "NotFound"
                result.errors.append(f"{source.title}: {'finnes ikke lenger' if missing else e}")
                log.warning("Looking up %s failed: %s", source.title, e)

        due = self.db.due()
        if self.cfg.max_downloads_per_run:
            due = due[: self.cfg.max_downloads_per_run]
        youtube_throttled = False
        last_provider = None
        for item in due:
            if stop and stop():
                break
            if item.provider == "youtube" and youtube_throttled:
                continue
            if last_provider:
                self._pause(item.provider)
            last_provider = item.provider
            source = self.db.source(item.source_id)
            label = f"{source.title}: {item.title}"
            if (when := providers.get(item.provider).ready_at(item)) is not None:
                self.db.postpone(item, max(when.timestamp(), time.time() + 5 * 60))
                log.info("%s isn't out yet, trying again %s", label, when.strftime("%d.%m %H:%M"))
                continue
            self.progress.__init__(label=label, source_id=source.id, started=time.time())
            try:
                outcome, payload = self.download(item)
            except Exception as e:
                status = _failure_status(item, e)
                if status == "waiting":
                    self.db.postpone(item, time.time() + NOT_YET_WAIT)
                    log.info("%s isn't out yet, trying again in %d minutes", label, NOT_YET_WAIT // 60)
                elif status == "throttled":
                    youtube_throttled = True
                    wait = min(THROTTLE_WAIT * 2 ** item.attempts, THROTTLE_WAIT_MAX)
                    self.db.postpone(item, time.time() + wait, error=f"YouTube ber oss vente: {e}"[:500])
                    log.warning("YouTube is throttling us; no more YouTube downloads this run (%s)", e)
                elif status == "failed":
                    self.db.set_status(item, status, error=str(e))
                    result.failed.append(label)
                    log.error("%s failed: %s", label, e)
                else:
                    self.db.set_status(item, status, error=str(e)[-500:])
                    log.warning("%s can't be downloaded (%s): %s", label, status, e)
                continue
            finally:
                self.progress.__init__()
            if outcome == "skip":
                self.db.set_status(item, "skipped", error=payload.reason)
                log.info("%s left out: %s", label, payload.reason)
                continue
            if outcome == "wait":
                self.db.postpone(item, payload.until)
                log.info("%s: %s, trying again later", label, payload.reason)
                continue
            files = payload
            self._save_details(item)
            self.db.set_status(item, "done", files=[str(f) for f in files], size=_size(files[0]))
            result.downloaded.append(label)
            result.changed.add(target(self.cfg, source, item).root)
            log.info("Downloaded %s", files[0])

        try:
            result.removed, folders = self.apply_retention()
            result.changed.update(folders)
        except Exception as e:
            log.warning("Clean-up failed: %s", e)
        return result

    def _save_details(self, item: Item) -> None:
        """What the video's own page told us (exact date, number, duration) into the DB."""
        with self.db.conn() as c:
            c.execute(
                "UPDATE items SET title = ?, plot = ?, aired = ?, season = ?, episode = ?, duration = ?, "
                "is_short = ?, is_live = ? WHERE provider = ? AND item_id = ?",
                (item.title, item.plot, item.aired.isoformat() if item.aired else None, item.season, item.episode,
                 item.duration, int(item.is_short), int(item.is_live), item.provider, item.item_id),
            )

    def _pause(self, provider: str) -> None:
        low, high = self.cfg.youtube_pause_seconds if provider == "youtube" else self.cfg.pause_seconds
        if high > 0:
            seconds = random.uniform(low, high)
            log.info("Pausing %.0f s before next download", seconds)
            time.sleep(seconds)


def _sidecars(t: Target) -> list[Path]:
    if not t.folder.is_dir():
        return []
    return [p for p in t.folder.iterdir() if p.name.startswith(t.basename + ".") or p.name == f"{t.basename}-thumb.jpg"]


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _failure_status(item: Item, e: Exception) -> str:
    message = str(e)
    if item.provider == "youtube":
        kind = youtube.classify(message)
        return {"login": "unavailable"}.get(kind, kind)
    if any(s in message for s in _NOT_YET):
        return "waiting"
    if isinstance(e, GeoRestrictedError) or any(s in message for s in _GEO):
        return "geo"
    if isinstance(e, DownloadError) and any(s in message for s in _EXPIRED):
        return "expired"
    return "failed"


def _fetch(url: str, out: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (mediafang)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    tmp = out.with_name(out.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(out)


def _backoff(n: int) -> float:
    """Seconds before yt-dlp's retry number n: 2, 4, 8 ... up to two minutes."""
    return min(2.0 ** (n + 1), 120.0)
