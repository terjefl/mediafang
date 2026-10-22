"""Podcast feeds for audio sources: /feed/<token>.xml, with the episodes served from
/feed/<token>/<id>.<ext>. The token is random and unguessable; it is what lets these paths be
open to podcast apps (which can't sign in through Cloudflare Access) while the rest isn't."""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime, time
from email.utils import format_datetime
from pathlib import Path

from .db import DB, Item, Source

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES)
TYPES = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".opus": "audio/ogg", ".mp4": "video/mp4", ".webm": "audio/webm"}


def media_file(item: Item) -> Path | None:
    for f in item.files:
        p = Path(f)
        if p.suffix.lower() in TYPES and p.exists():
            return p
    return None


def render(db: DB, source: Source, base_url: str) -> bytes:
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = source.folder_name
    ET.SubElement(ch, "link").text = source.url or base_url
    ET.SubElement(ch, "description").text = source.plot or source.title
    ET.SubElement(ch, "language").text = "no"
    if source.poster or source.image:
        ET.SubElement(ch, f"{{{ITUNES}}}image", href=source.poster or source.image)
    ET.SubElement(ch, f"{{{ITUNES}}}author").text = source.title
    for item in db.items(source.id):
        if item.status != "done" or not (path := media_file(item)):
            continue
        entry = ET.SubElement(ch, "item")
        ET.SubElement(entry, "title").text = item.title
        ET.SubElement(entry, "description").text = item.plot
        ET.SubElement(entry, "guid", isPermaLink="false").text = f"{source.provider}:{item.item_id}"
        if item.aired:
            when = datetime.combine(item.aired, time(12), tzinfo=UTC)
            ET.SubElement(entry, "pubDate").text = format_datetime(when)
        url = f"{base_url}/feed/{source.feed_token}/{item.item_id}{path.suffix}"
        ET.SubElement(entry, "enclosure", url=url, length=str(path.stat().st_size), type=TYPES[path.suffix.lower()])
        if item.duration:
            ET.SubElement(entry, f"{{{ITUNES}}}duration").text = str(item.duration)
        if item.image:
            ET.SubElement(entry, f"{{{ITUNES}}}image", href=item.image)
    ET.indent(rss)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)
