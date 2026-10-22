"""YouTube channels and playlists.

Listing is cheap on purpose: a flat listing of the channel's tabs (one paged request per tab, not
one per video, which is what makes Pinchflat's indexing slow) at most once a day, and the
channel's RSS feed (one request, the latest ~15 uploads) on the runs in between. The exact
upload date, duration and whether a video is a short or a livestream are only certain once
the video itself is looked up, right before downloading; filters are checked again then.
"""

import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from yt_dlp import YoutubeDL

from ..config import Config
from ..db import DB, Item, Source
from . import Hit, Info, date_episode

log = logging.getLogger(__name__)

FULL_INDEX_SECONDS = 24 * 3600
# An incremental listing (newest first) stops after this many known videos in a row
STOP_AFTER_KNOWN = 30
# Pinchflat numbers a day's uploads 99, 98, ...; new files continue that in the same seasons
FIRST_INDEX = 99

_CHANNEL_ID = re.compile(r"youtube\.com/channel/(UC[\w-]{22})", re.IGNORECASE)
_HANDLE = re.compile(r"youtube\.com/(@[\w.\-%]+)", re.IGNORECASE)
_LEGACY = re.compile(r"youtube\.com/((?:c|user)/[\w.\-%]+)", re.IGNORECASE)
_PLAYLIST = re.compile(r"[?&]list=((?:PL|UU|OL|FL)[\w-]+)")


def parse_url(text: str) -> tuple[str, str] | None:
    """("channel", "UC..." / "@handle" / "c/name") or ("playlist", "PL...") for a YouTube link."""
    text = text.strip()
    if "youtube.com" not in text and "youtu.be" not in text:
        return None
    if m := _PLAYLIST.search(text):
        return "playlist", m[1]
    for pattern in (_CHANNEL_ID, _HANDLE, _LEGACY):
        if m := pattern.search(text):
            return "channel", urllib.parse.unquote(m[1])
    return None


def source_url(kind: str, ref: str) -> str:
    if kind == "playlist":
        return f"https://www.youtube.com/playlist?list={ref}"
    if ref.startswith("UC"):
        return f"https://www.youtube.com/channel/{ref}"
    return f"https://www.youtube.com/{ref}"


def page_url(source: Source, item: Item | None = None) -> str:
    if item:
        return f"https://www.youtube.com/watch?v={item.item_id}"
    return source.url or source_url(source.kind, source.ref)


def video_url(item: Item) -> str:
    return f"https://www.youtube.com/watch?v={item.item_id}"


def ready_at(item: Item) -> datetime | None:
    return None  # only the video's own page says; see prepare()


def _ydl(cfg: Config | None = None, **extra) -> YoutubeDL:
    params = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "logger": _QuietLogger(), **extra}
    if cfg:
        params.update(_common(cfg))
    return YoutubeDL(params)


class _QuietLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.debug(msg)

    def error(self, msg):
        log.warning(msg)


def _common(cfg: Config) -> dict:
    params: dict = {"sleep_interval_requests": 0.75}
    if cfg.pot_provider_url:
        params["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [cfg.pot_provider_url]}}
    return params


# ---- looking things up ---------------------------------------------------------


def _thumb(info: dict, wanted: str) -> str | None:
    for t in info.get("thumbnails") or []:
        if t.get("id") == wanted:
            return t.get("url")
    return None


def lookup(kind: str, ref: str) -> Info:
    url = source_url(kind, ref)
    with _ydl(playlistend=1) as ydl:
        info = ydl.extract_info(url if kind == "playlist" else url + "/videos", download=False)
    if kind == "playlist":
        thumbs = info.get("thumbnails") or []
        image = thumbs[-1]["url"] if thumbs else None
        return Info(
            "youtube", kind, info.get("id") or ref, info.get("title") or ref, info.get("description") or "",
            image=image, poster=image, backdrop=image, url=source_url(kind, info.get("id") or ref),
        )
    channel_id = info.get("channel_id") or ref
    avatar = _thumb(info, "avatar_uncropped")
    banner = _thumb(info, "banner_uncropped")
    return Info(
        "youtube", "channel", channel_id, info.get("channel") or info.get("uploader") or ref,
        info.get("description") or "", image=banner or avatar, poster=avatar, backdrop=banner,
        url=source_url("channel", channel_id),
    )


def search(query: str) -> list[Hit]:
    """Channels matching the query, found through the videos YouTube's search returns."""
    with _ydl() as ydl:
        info = ydl.extract_info(f"ytsearch20:{query}", download=False)
    seen, hits = set(), []
    for e in info.get("entries") or []:
        cid = e.get("channel_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        thumbs = e.get("thumbnails") or []
        hits.append(
            Hit("youtube", "channel", cid, e.get("channel") or e.get("uploader") or cid,
                f"Kanal · f.eks. «{e.get('title', '')}»", thumbs[-1]["url"] if thumbs else None)
        )
    return hits


# ---- finding videos ------------------------------------------------------------


@dataclass
class Entry:
    video_id: str
    title: str
    published: datetime | None  # approximate in flat listings, exact from RSS
    duration: int | None
    is_short: bool
    is_live: bool
    image: str | None
    description: str = ""


def _tabs(source: Source) -> list[tuple[str, bool, bool]]:
    """(url, is_short, is_live) for the lists to read."""
    base = source.url or source_url(source.kind, source.ref)
    if source.kind == "playlist":
        return [(base, False, False)]
    tabs = [(base + "/videos", False, False)]
    if source.livestreams:
        tabs.append((base + "/streams", False, True))
    if source.shorts:
        tabs.append((base + "/shorts", True, False))
    return tabs


def oldest_wanted(source: Source) -> date | None:
    """Nothing published before this date is wanted (start date, 'only new', keep_days)."""
    limits = [source.cutoff]
    if source.mode == "new":
        limits.append(source.since.date())
    if source.keep_days:
        limits.append(date.today() - timedelta(days=source.keep_days))
    limits = [d for d in limits if d]
    return max(limits) if limits else None


def list_flat(cfg: Config, source: Source, known: set[str]) -> list[Entry]:
    """Every video in the source's lists, newest first; with known ids, stop at a run of them.
    A channel's tabs are newest first, so they also stop past the oldest date wanted (with a
    week to spare: flat listings only know dates roughly, "3 weeks ago")."""
    found: list[Entry] = []
    oldest = oldest_wanted(source)
    stop_at = oldest - timedelta(days=7) if oldest and source.kind == "channel" else None
    for url, is_short, is_live in _tabs(source):
        with _ydl(cfg, extractor_args={"youtubetab": {"approximate_date": [""]}}) as ydl:
            try:
                info = ydl.extract_info(url, download=False, process=False)
            except Exception as e:
                if "does not have a" in str(e):  # e.g. a channel without a Live tab
                    continue
                raise
            in_a_row = 0
            for e in info.get("entries") or []:
                vid = e.get("id")
                if not vid:
                    continue
                if vid in known:
                    in_a_row += 1
                    if known and source.kind == "channel" and in_a_row >= STOP_AFTER_KNOWN:
                        break
                    continue
                in_a_row = 0
                ts = e.get("timestamp")
                if stop_at and ts and datetime.fromtimestamp(ts, UTC).date() < stop_at:
                    break
                thumbs = e.get("thumbnails") or []
                found.append(
                    Entry(
                        vid, e.get("title") or vid, datetime.fromtimestamp(ts, UTC) if ts else None,
                        e.get("duration"), is_short, is_live or e.get("live_status") in ("was_live", "is_live"),
                        thumbs[-1]["url"] if thumbs else None,
                    )
                )
    return found


_ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
         "media": "http://search.yahoo.com/mrss/"}


def parse_feed(xml: bytes) -> list[Entry]:
    root = ET.fromstring(xml)
    entries = []
    for e in root.findall("a:entry", _ATOM):
        vid = e.findtext("yt:videoId", namespaces=_ATOM)
        if not vid:
            continue
        published = e.findtext("a:published", namespaces=_ATOM)
        thumb = e.find("media:group/media:thumbnail", _ATOM)
        link = e.find("a:link", _ATOM)
        entries.append(
            Entry(
                vid, e.findtext("a:title", namespaces=_ATOM) or vid,
                datetime.fromisoformat(published) if published else None, None,
                "/shorts/" in (link.get("href", "") if link is not None else ""), False,
                thumb.get("url") if thumb is not None else None,
                e.findtext("media:group/media:description", namespaces=_ATOM) or "",
            )
        )
    return entries


def list_feed(source: Source) -> list[Entry]:
    key = "playlist_id" if source.kind == "playlist" else "channel_id"
    url = f"https://www.youtube.com/feeds/videos.xml?{key}={source.ref}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return parse_feed(resp.read())


def filter_reason(source: Source, *, title: str, published: date | None, duration: int | None,
                  is_short: bool, is_live: bool) -> str | None:
    """Why a video is left out by the source's settings, or None."""
    if is_short and not source.shorts:
        return "short"
    if is_live and not source.livestreams:
        return "livestream"
    if published and source.cutoff and published < source.cutoff:
        return f"før {source.cutoff:%d.%m.%Y}"
    if published and source.mode == "new" and published < source.since.date():
        return "fra før du begynte å følge"
    if published and source.keep_days and published < date.today() - timedelta(days=source.keep_days):
        return f"eldre enn {source.keep_days} dager"
    if source.title_regex:
        try:
            if not re.search(source.title_regex, title, re.IGNORECASE):
                return "tittelfilter"
        except re.error:
            pass
    if duration is not None:
        if source.min_duration and duration < source.min_duration:
            return "for kort"
        if source.max_duration and duration > source.max_duration:
            return "for lang"
    return None


def scan(db: DB, source: Source, cfg: Config) -> int:
    """Record new videos. A full flat listing the first time and once a day, the RSS feed in
    between. Videos the settings leave out are recorded as skipped, so they aren't looked at
    again (changing the settings clears them)."""
    if not (source.image or source.poster):
        # e.g. taken over from Pinchflat: the channel's avatar and banner, for the page and Plex
        try:
            info = lookup(source.kind, source.ref)
            db.update_meta(source.id, image=info.image, poster=info.poster, backdrop=info.backdrop,
                           plot=source.plot or info.plot)
        except Exception as e:  # cosmetic; never fail a scan over it
            log.warning("%s: could not look up the channel's pictures: %s", source.title, e)
    known = db.known_ids(source.id)
    full = not known or time.time() - source.last_full_index > FULL_INDEX_SECONDS
    if full:
        entries = list_flat(cfg, source, known)
    else:
        try:
            entries = [e for e in list_feed(source) if e.video_id not in known]
        except Exception as e:  # the feed has had outages; the daily listing catches up
            log.warning("%s: RSS feed failed (%s), listing instead", source.title, e)
            entries, full = list_flat(cfg, source, known), True
    new = 0
    for e in entries:
        day = e.published.date() if e.published else date.today()
        reason = filter_reason(
            source, title=e.title, published=e.published.date() if e.published else None,
            duration=e.duration, is_short=e.is_short, is_live=e.is_live,
        )
        item = Item(
            provider="youtube", item_id=e.video_id, source_id=source.id, season=day.year,
            episode=date_episode(day, FIRST_INDEX), title=e.title, plot=e.description, aired=day,
            image=e.image, duration=e.duration, is_short=e.is_short, is_live=e.is_live,
            status="skipped" if reason else "pending", error=reason,
        )
        new += db.add_item(item, unique_number=True, number_step=-1) and not reason
    db.touch(source.id, full_index=full)
    if new:
        log.info("%s: %d new video(s)", source.title, new)
    return new


# ---- downloading ---------------------------------------------------------------


def ytdlp_options(cfg: Config, source: Source, item: Item) -> dict:
    opts = _common(cfg)
    if source.quality == 0:
        opts.update(format="ba[ext=m4a]/ba", final_ext="m4a",
                    postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}])
    else:
        opts.update(
            format="bv*+ba/b",
            format_sort=[f"res:{source.quality}", "vcodec:avc", "acodec:m4a"],
            merge_output_format="mp4",
        )
    if source.subtitles:
        opts.update(writesubtitles=True, subtitleslangs=[s.strip() for s in source.subtitles.split(",") if s.strip()])
    if source.sponsorblock:
        cats = [c.strip() for c in source.sb_categories.split(",") if c.strip()] or ["sponsor"]
        opts.setdefault("postprocessors", [])
        opts["postprocessors"] += [
            {"key": "SponsorBlock", "categories": cats, "when": "after_filter"},
            {"key": "ModifyChapters", "remove_sponsor_segments": cats if source.sponsorblock == "remove" else [],
             "sponsorblock_chapter_title": "[SponsorBlock]: %(category_names)l"},
        ]
    if source.cookies and cfg.cookies_file:
        work_copy = cfg.data_dir / "cookies.txt"
        if cfg.cookies_file.exists() and (
            not work_copy.exists() or cfg.cookies_file.stat().st_mtime > work_copy.stat().st_mtime
        ):
            work_copy.write_bytes(cfg.cookies_file.read_bytes())
        opts["cookiefile"] = str(work_copy)
    return opts


@dataclass
class Decision:
    action: str  # ok | skip | wait
    reason: str = ""
    until: float = 0.0


def prepare(source: Source, item: Item, info: dict) -> Decision:
    """Look at the video itself before downloading: not out yet, or left out by a filter
    that the listing couldn't check. Fills in the exact date and duration on item."""
    status = info.get("live_status")
    if status in ("is_upcoming", "is_live", "post_live"):
        start = info.get("release_timestamp")
        until = max(time.time() + 1800, (start or 0) + 3 * 3600 if status == "is_upcoming" else 0)
        return Decision("wait", "ikke ferdig sendt ennå" if status != "is_upcoming" else "premiere senere", until)
    if info.get("upload_date"):
        day = datetime.strptime(info["upload_date"], "%Y%m%d").date()
        if day != item.aired:
            item.aired, item.season, item.episode = day, day.year, date_episode(day, FIRST_INDEX)
    item.duration = int(info["duration"]) if info.get("duration") else item.duration
    item.is_live = item.is_live or status == "was_live"
    item.is_short = item.is_short or _looks_short(info)
    item.plot = info.get("description") or item.plot
    item.title = info.get("title") or item.title
    reason = filter_reason(
        source, title=item.title, published=item.aired, duration=item.duration,
        is_short=item.is_short, is_live=item.is_live,
    )
    return Decision("skip", reason) if reason else Decision("ok")


def _looks_short(info: dict) -> bool:
    url = info.get("original_url") or info.get("webpage_url") or ""
    if "/shorts/" in url or info.get("media_type") == "short":
        return True
    w, h, d = info.get("width"), info.get("height"), info.get("duration")
    return bool(w and h and h > w and d and d <= 180)


# Messages from YouTube (through yt-dlp) and what they mean for us
GONE = ("Private video", "Video unavailable", "This video has been removed", "account associated with this video",
        "members-only", "Join this channel", "This video is available to this channel's members")
NEEDS_LOGIN = ("Sign in to confirm your age", "age-restricted")
THROTTLED = ("HTTP Error 429", "Too Many Requests", "Sign in to confirm you", "content isn't available, try again",
             "rate-limited")


def classify(message: str) -> str:
    """'unavailable', 'throttled', 'login' or 'failed' for a download error."""
    if any(s in message for s in THROTTLED):
        return "throttled"
    if any(s in message for s in NEEDS_LOGIN):
        return "login"
    if any(s in message for s in GONE):
        return "unavailable"
    return "failed"
