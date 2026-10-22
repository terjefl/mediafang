"""Find new episodes of what we follow, download them, and tidy up after date series."""

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

from . import planner, psapi
from .config import Config
from .db import DB, EpisodeRow, Follow
from .library import (
    Target,
    show_folder,
    subtitle_name,
    target,
    write_episode_nfo,
    write_movie_nfo,
    write_show_nfo,
)
from .poster import make_poster

log = logging.getLogger(__name__)

# yt-dlp's NRK messages for rights that are gone for good; "not ready" is worth another try
_EXPIRED = ("har gått ut", "Ikke tilgjengelig", "ProgramRightsHasExpired", "NoProgramRights")
_GEO = ("utenfor Norge", "IsGeoBlocked")
# ...and for something not out yet ("NRK said: Kommer i dag kl. 21:11", or still on air)
_NOT_YET = ("NRK said: Kommer", "ProgramRightsAreNotReady", "ikke se eller høre programmet")
NOT_YET_WAIT = 30 * 60
VIDEO_SUFFIXES = (".mp4", ".mkv", ".m4v", ".webm")


@dataclass
class RunResult:
    found: int = 0
    downloaded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    removed: int = 0
    errors: list[str] = field(default_factory=list)
    tv_changed: bool = False
    movies_changed: bool = False


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
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        self.tmp_dir = cfg.data_dir / "tmp"

    # ---- finding episodes ----------------------------------------------------

    def scan(self, follow: Follow) -> int:
        """Look up what NRK has for a follow and record new episodes. Returns how many."""
        if follow.kind == "program":
            p = psapi.program(follow.ref)
            self.db.update_meta(follow.id, plot=p.plot, image=p.image, poster=p.poster, backdrop=p.backdrop)
            if not p.available:
                return 0
            row = EpisodeRow(
                prf_id=p.prf_id, follow_id=follow.id, season=0, episode=0, title=p.title, plot=p.plot,
                aired=p.aired, expires=p.expires, image=p.image, year=p.year, original_title=p.original_title,
                status="pending", attempts=0, error=None, retry_after=0,
            )
            return int(self.db.add_episode(row))

        series = psapi.series(follow.ref)
        self.db.update_meta(
            follow.id, plot=series.plot, series_type=series.series_type,
            image=series.image, poster=series.poster, backdrop=series.backdrop,
        )
        follow = self.db.follow(follow.id)
        new = 0
        for ref in planner.seasons_to_scan(follow, series, self.db.scanned(follow.id)):
            for position, inst in enumerate(psapi.season(series.slug, ref.name)):
                if planner.wanted(follow, inst):
                    row = planner.plan(follow, series, ref, position, inst)
                    new += self.db.add_episode(row, unique_number=series.dated)
            self.db.mark_scanned(follow.id, ref.name)
        if new:
            log.info("%s: %d new episode(s)", follow.title, new)
        return new

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
            **extra,
        }

    def download(self, ep: EpisodeRow) -> list[Path]:
        follow = self.db.follow(ep.follow_id)
        t = target(self.cfg, follow, ep)
        t.folder.mkdir(parents=True, exist_ok=True)
        # Plex scans the moment the video lands, and its NFO agent identifies the item by the
        # metadata it finds then. So every sidecar must exist before the video does.
        written: list[Path] = [t.nfo]
        try:
            if follow.kind == "program":
                write_movie_nfo(t.nfo, ep)
                self.ensure_art(follow, t.root, ep.image)
            else:
                self.ensure_art(follow, t.root, ep.image)
                write_episode_nfo(t.nfo, follow, ep)
                written += self._episode_thumbs(t, ep.image)
            video = self._fetch_video(follow, ep, t, written)
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return [video, *[p for p in written if p.exists() and p != video]]

    def _fetch_video(self, follow: Follow, ep: EpisodeRow, t: Target, written: list[Path]) -> Path:
        params = self._params(
            paths={"home": str(t.folder), "temp": str(self.tmp_dir)},
            outtmpl={"default": f"{t.basename}.%(ext)s"},
            format="bv*+ba/b",
            merge_output_format="mp4",
            writesubtitles=True,
            subtitleslangs=["all"],
            postprocessors=[
                {"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"},
                {"key": "FFmpegMetadata", "add_metadata": True},
            ],
        )
        with YoutubeDL(params) as ydl:
            info = ydl.extract_info(f"nrk:{ep.prf_id}", download=False)
            # What ends up in the file's own tags (Infuse, VLC and friends read these)
            info.update(title=ep.title, description=ep.plot)
            if follow.kind == "series":
                info.update(series=follow.title, season_number=ep.season, episode_number=ep.episode)
            result = ydl.process_ie_result(info, download=True)
        video = Path(result["requested_downloads"][0]["filepath"])
        written.append(video)
        written += self._rename_subtitles(t)
        return video

    def _rename_subtitles(self, t: Target) -> list[Path]:
        """yt-dlp names subtitles <base>.<nrk track>.srt; give them names the servers understand."""
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

    def _episode_thumbs(self, t: Target, url: str | None) -> list[Path]:
        """The episode's still: <base>.jpg for Plex, <base>-thumb.jpg for Jellyfin."""
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

    def ensure_art(self, follow: Follow, root: Path, fallback_image: str | None = None, force: bool = False) -> None:
        """tvshow.nfo, poster.jpg and fanart.jpg in a show's folder (a film's folder gets the
        pictures). NRK has 2:3 posters for some titles only; the others get one made from their
        16:9 picture. Cosmetic: never fails."""
        try:
            root.mkdir(parents=True, exist_ok=True)
            if follow.kind == "series":
                write_show_nfo(root / "tvshow.nfo", follow)
            wide = follow.backdrop or follow.image or fallback_image
            fanart = root / "fanart.jpg"
            if wide and (force or not fanart.exists()):
                _fetch(wide, fanart)
            poster = root / "poster.jpg"
            if force or not poster.exists():
                if follow.poster:
                    _fetch(follow.poster, poster)
                elif image := follow.image or fallback_image:
                    self.tmp_dir.mkdir(parents=True, exist_ok=True)
                    src = self.tmp_dir / f"poster-src-{follow.id}.jpg"
                    _fetch(image, src)
                    make_poster(src, follow.title, "NRK", poster)
                    src.unlink(missing_ok=True)
        except Exception as e:
            log.warning("Could not update artwork for %s: %s", follow.title, e)

    def refresh_art(self) -> int:
        """Rewrite every followed show's tvshow.nfo, poster and background."""
        n = 0
        for follow in self.db.follows():
            if follow.kind == "series":
                self.ensure_art(follow, show_folder(self.cfg, follow), force=True)
                n += 1
        return n

    # ---- date series clean-up ------------------------------------------------

    def apply_retention(self) -> int:
        """Delete episodes of date series older than their follow's keep_days."""
        removed = 0
        for follow in self.db.follows():
            if not (follow.dated and follow.keep_days):
                continue
            cutoff = date.today() - timedelta(days=follow.keep_days)
            for ep in self.db.expired_done(follow.id, cutoff):
                t = target(self.cfg, follow, ep)
                for path in [Path(f) for f in ep.files] + _sidecars(t):
                    path.unlink(missing_ok=True)
                self.db.set_status(ep.prf_id, "removed")
                removed += 1
                log.info("%s: removed %s (older than %d days)", follow.title, t.basename, follow.keep_days)
            season_dirs = {target(self.cfg, follow, ep).folder for ep in self.db.episodes(follow.id)}
            for folder in season_dirs:
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
        return removed

    # ---- one run -------------------------------------------------------------

    def run(self, stop=None) -> RunResult:
        result = RunResult()
        for follow in self.db.follows(enabled_only=True):
            try:
                result.found += self.scan(follow)
            except psapi.NotFound:
                result.errors.append(f"{follow.title}: finnes ikke lenger hos NRK")
                log.warning("%s (%s) is gone from NRK", follow.title, follow.ref)
            except Exception as e:
                result.errors.append(f"{follow.title}: {e}")
                log.warning("Looking up %s failed: %s", follow.title, e)

        due = self.db.due()
        if self.cfg.max_downloads_per_run:
            due = due[: self.cfg.max_downloads_per_run]
        for i, ep in enumerate(due):
            if stop and stop():
                break
            if i:
                self._pause()
            follow = self.db.follow(ep.follow_id)
            label = f"{follow.title} {ep.title}"
            if (when := psapi.ready_at(ep.prf_id)) is not None:
                self.db.postpone(ep.prf_id, max(when.timestamp(), time.time() + 5 * 60))
                log.info("%s isn't out at NRK yet, trying again %s", label, when.strftime("%d.%m %H:%M"))
                continue
            try:
                files = self.download(ep)
            except Exception as e:
                status = _failure_status(e)
                if status == "waiting":
                    self.db.postpone(ep.prf_id, time.time() + NOT_YET_WAIT)
                    log.info("%s isn't out at NRK yet, trying again in %d minutes", label, NOT_YET_WAIT // 60)
                    continue
                self.db.set_status(ep.prf_id, status, error=str(e))
                if status == "failed":
                    result.failed.append(label)
                    log.error("%s failed: %s", label, e)
                else:
                    log.warning("%s can't be downloaded (%s): %s", label, status, e)
                continue
            self.db.set_status(ep.prf_id, "done", files=[str(f) for f in files])
            result.downloaded.append(label)
            if follow.kind == "program":
                result.movies_changed = True
            else:
                result.tv_changed = True
            log.info("Downloaded %s", files[0])

        try:
            result.removed = self.apply_retention()
            result.tv_changed |= bool(result.removed)
        except Exception as e:
            log.warning("Clean-up failed: %s", e)
        return result

    def _pause(self) -> None:
        low, high = self.cfg.pause_seconds
        if high > 0:
            seconds = random.uniform(low, high)
            log.info("Pausing %.0f s before next download", seconds)
            time.sleep(seconds)


def _sidecars(t: Target) -> list[Path]:
    if not t.folder.is_dir():
        return []
    return [p for p in t.folder.iterdir() if p.name.startswith(t.basename + ".") or p.name == f"{t.basename}-thumb.jpg"]


def _failure_status(e: Exception) -> str:
    message = str(e)
    if any(s in message for s in _NOT_YET):
        return "waiting"
    if isinstance(e, GeoRestrictedError) or any(s in message for s in _GEO):
        return "geo"
    if isinstance(e, DownloadError) and any(s in message for s in _EXPIRED):
        return "expired"
    return "failed"


def _fetch(url: str, out: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": psapi.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    tmp = out.with_name(out.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(out)


def _backoff(n: int) -> float:
    """Seconds before yt-dlp's retry number n: 2, 4, 8 ... up to two minutes."""
    return min(2.0 ** (n + 1), 120.0)
