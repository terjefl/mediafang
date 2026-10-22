"""One-off downloads, what MeTube did: a link to a single video (YouTube or anything else yt-dlp
knows), as video or as audio only. They belong to two fixed sources:

    Enkeltvideoer  <youtube_dir>/Enkeltvideoer/<channel>/<YYYY-MM-DD> <title>/<title> [<id>].mp4
    Enkeltlyd      <youtube_dir>/Lyd/Enkelt/<YYYY-MM-DD> <title> [<id>].m4a   (+ a podcast feed)
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

from yt_dlp import YoutubeDL

from ..config import Config
from ..db import DB, Item, Source

log = logging.getLogger(__name__)

SOURCES = {
    "video": {"title": "Enkeltvideoer", "layout": "videos", "library": "Enkeltvideoer", "folder": "", "quality": 2160},
    "audio": {"title": "Enkeltlyd", "layout": "audio", "library": "Lyd", "folder": "Enkelt", "quality": 0},
}
# A link to a list adds at most this many of its videos
MAX_FROM_LIST = 100


def parse_url(text: str) -> None:
    return None  # nothing to follow


def scan(db: DB, source: Source, cfg: Config | None = None) -> int:
    return 0  # items come from add()


def ready_at(item: Item) -> None:
    return None


def video_url(item: Item) -> str:
    return item.url or ""


def page_url(source: Source, item: Item | None = None) -> str:
    return (item.url or "") if item else ""


def ytdlp_options(cfg: Config, source: Source, item: Item) -> dict:
    if source.quality == 0:
        return {"format": "ba[ext=m4a]/ba", "final_ext": "m4a",
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]}
    return {"format": "bv*+ba/b", "format_sort": [f"res:{source.quality}", "vcodec:avc", "acodec:m4a"],
            "merge_output_format": "mp4"}


def source_for(db: DB, kind: str) -> Source:
    """The fixed source for one-off video or audio downloads, made the first time it's needed."""
    if s := db.source_by_ref("adhoc", kind):
        return s
    spec = dict(SOURCES[kind])
    return db.save_source("adhoc", kind, kind, spec.pop("title"), mode="all", **spec)


def item_id(info: dict, audio: bool = False) -> str:
    """Unique and safe in URLs and file names: 'youtube-dQw4w9WgXcQ', 'vimeo-123456'; the audio
    version of the same video is its own item ('youtube-dQw4w9WgXcQ-lyd')."""
    key = (info.get("extractor_key") or info.get("ie_key") or "web").lower()
    return re.sub(r"[^\w-]", "_", f"{key}-{info.get('id') or 'x'}") + ("-lyd" if audio else "")


@dataclass
class Added:
    items: list[Item]
    known: list[Item]  # asked for before; failed ones are tried again

    @property
    def message(self) -> str:
        if len(self.items) + len(self.known) == 1:
            item = (self.items or self.known)[0]
            if self.known and item.status == "done":
                return f"Har den allerede: {item.title}"
            return f"Lastes ned: {item.title}"
        return f"{len(self.items)} lagt i kø" + (f", {len(self.known)} hadde vi fra før" if self.known else "")


def add(db: DB, url: str, audio: bool = False) -> Added:
    """Queue a link for download. A list (playlist) adds its videos."""
    source = source_for(db, "audio" if audio else "video")
    with YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "noplaylist": True,
                    "playlistend": MAX_FROM_LIST}) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [e for e in info.get("entries") or [] if e] if info.get("_type") == "playlist" else [info]
    added = Added([], [])
    for e in entries:
        item = _item(source, e, url if len(entries) == 1 else None, audio)
        if db.add_item(item):
            added.items.append(item)
        else:
            known = db.item("adhoc", item.item_id)
            if known.status not in ("done", "pending"):
                db.retry("adhoc", known.item_id)
            added.known.append(known)
    return added


def _item(source: Source, info: dict, url: str | None, audio: bool) -> Item:
    day = _day(info)
    thumbs = info.get("thumbnails") or []
    return Item(
        provider="adhoc", item_id=item_id(info, audio), source_id=source.id, season=day.year if day else 0, episode=0,
        title=info.get("title") or info.get("id") or "Uten tittel", plot=info.get("description") or "", aired=day,
        image=info.get("thumbnail") or (thumbs[-1]["url"] if thumbs else None),
        duration=int(info["duration"]) if info.get("duration") else None,
        url=info.get("webpage_url") or info.get("url") if info.get("_type") == "url" or not url else url,
        channel=info.get("channel") or info.get("uploader") or info.get("playlist_uploader"),
    )


def prepare(item: Item, info: dict) -> None:
    """What the video's own page says, before the files are named after it."""
    item.title = info.get("title") or item.title
    item.plot = info.get("description") or item.plot
    item.aired = _day(info) or item.aired or date.today()
    item.season = item.aired.year
    item.duration = int(info["duration"]) if info.get("duration") else item.duration
    item.channel = info.get("channel") or info.get("uploader") or item.channel or info.get("extractor_key")
    item.image = item.image or info.get("thumbnail")


def _day(info: dict) -> date | None:
    if info.get("upload_date"):
        return datetime.strptime(info["upload_date"], "%Y%m%d").date()
    if info.get("timestamp"):
        return datetime.fromtimestamp(info["timestamp"]).date()
    return None
