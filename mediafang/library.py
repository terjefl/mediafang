"""Library layout and Kodi-style NFO sidecars, which both Plex (its NFO agents) and Jellyfin read.

NRK:
    <library_dir>/Fleksnes/tvshow.nfo, poster.jpg, fanart.jpg
    <library_dir>/Fleksnes/Season 01/Fleksnes - S01E01 - Fysiske fordeler.mp4 | .nfo | .nb.srt | .jpg
    <library_dir>/Dagsrevyen/Season 2026/Dagsrevyen - S2026E102201 - 2026-10-22.mp4
    <movies_dir>/Kongehusets hemmelige millioner (2024)/Kongehusets hemmelige millioner (2024).mp4 | .nfo

YouTube, under <youtube_dir>/<library>/<channel>/, the same as Pinchflat made them:
    tv:     Season 2026/s2026e102299 - <title> [<id>].mp4 | .nfo | -thumb.jpg   (+ tvshow.nfo, poster.jpg)
    videos: 2026-10-19 <title>/<title> [<id>].mp4 | -thumb.jpg
    audio:  2026-10-19 <title> [<id>].m4a | -thumb.jpg
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import Item, Source
from .titles import date_title_suffix, safe_filename

DEFAULT_LIBRARY = {"tv": "TV", "videos": "Video", "audio": "Lyd"}
LAYOUTS = {"tv": "Serie (TV)", "videos": "Videoer", "audio": "Bare lyd", "movie": "Film"}


@dataclass
class Target:
    root: Path  # show / channel folder, or a film's own folder
    folder: Path  # where the video goes
    basename: str  # file name without extension

    @property
    def nfo(self) -> Path:
        return self.folder / f"{self.basename}.nfo"


def season_dir(season: int) -> str:
    return f"Season {season:02d}" if season < 1000 else f"Season {season}"


def source_root(cfg: Config, source: Source) -> Path:
    """The source's own folder: a show's folder, a channel's folder, or (NRK films) movies_dir."""
    if source.provider == "youtube":
        base = cfg.youtube_dir or cfg.library_dir.parent / "YouTube"
        library = source.library or DEFAULT_LIBRARY.get(source.layout, "Video")
        return base / safe_filename(library) / safe_filename(source.folder_name)
    if source.kind == "program":
        return cfg.movies_dir
    return cfg.library_dir / safe_filename(source.folder_name)


def target(cfg: Config, source: Source, item: Item) -> Target:
    if source.provider == "youtube":
        return _youtube_target(cfg, source, item)
    if source.kind == "program":
        year = item.year or (item.aired.year if item.aired else None)
        name = safe_filename(f"{item.title} ({year})" if year else item.title)
        root = cfg.movies_dir / name
        return Target(root, root, name)
    show = safe_filename(source.folder_name)
    root = cfg.library_dir / show
    if source.dated:
        name = f"{show} - S{item.season}E{item.episode:06d} - {item.aired.isoformat() if item.aired else item.title}"
        if extra := date_title_suffix(item.title):
            name += f" - {safe_filename(extra, 80)}"
    else:
        name = f"{show} - S{item.season:02d}E{item.episode:02d}"
        if item.title:
            name += f" - {safe_filename(item.title, 100)}"
    return Target(root, root / season_dir(item.season), safe_filename(name, 200))


def _youtube_target(cfg: Config, source: Source, item: Item) -> Target:
    root = source_root(cfg, source)
    title = safe_filename(item.title, 150)
    day = item.aired.isoformat() if item.aired else "0000-00-00"
    if source.layout == "tv":
        return Target(root, root / season_dir(item.season), f"s{item.season}e{item.episode:06d} - {title} [{item.item_id}]")
    if source.layout == "audio":
        return Target(root, root, f"{day} {title} [{item.item_id}]")
    return Target(root, root / safe_filename(f"{day} {title}", 180), f"{title} [{item.item_id}]")


def needs_nfo(source: Source) -> bool:
    return source.layout in ("tv", "movie")


def write_episode_nfo(path: Path, source: Source, item: Item) -> None:
    root = ET.Element("episodedetails")
    ET.SubElement(root, "title").text = item.title or f"Episode {item.episode}"
    ET.SubElement(root, "showtitle").text = source.title if source.provider == "nrk" else source.folder_name
    ET.SubElement(root, "season").text = str(item.season)
    ET.SubElement(root, "episode").text = str(item.episode)
    if item.aired:
        # A bare date: Plex reads a date with a time as no date at all
        ET.SubElement(root, "aired").text = item.aired.isoformat()
        ET.SubElement(root, "premiered").text = item.aired.isoformat()
    ET.SubElement(root, "plot").text = item.plot
    if item.duration:
        ET.SubElement(root, "runtime").text = str(round(item.duration / 60))
    if source.provider == "youtube":
        ET.SubElement(root, "genre").text = "YouTube"
    ET.SubElement(root, "uniqueid", type=source.provider, default="true").text = item.item_id
    _write_xml(root, path)


def write_show_nfo(path: Path, source: Source) -> None:
    root = ET.Element("tvshow")
    ET.SubElement(root, "title").text = source.title if source.provider == "nrk" else source.folder_name
    ET.SubElement(root, "plot").text = source.plot
    ET.SubElement(root, "studio").text = "NRK" if source.provider == "nrk" else source.title
    ET.SubElement(root, "uniqueid", type=source.provider, default="true").text = source.ref
    _write_xml(root, path)


def write_movie_nfo(path: Path, item: Item) -> None:
    root = ET.Element("movie")
    ET.SubElement(root, "title").text = item.title
    if item.original_title and item.original_title != item.title:
        ET.SubElement(root, "originaltitle").text = item.original_title
    if item.year:
        ET.SubElement(root, "year").text = str(item.year)
    if item.aired:
        ET.SubElement(root, "premiered").text = item.aired.isoformat()
    ET.SubElement(root, "plot").text = item.plot
    ET.SubElement(root, "studio").text = "NRK"
    ET.SubElement(root, "uniqueid", type="nrk", default="true").text = item.item_id
    _write_xml(root, path)


def _write_xml(root: ET.Element, path: Path) -> None:
    ET.indent(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def subtitle_name(basename: str, lang: str) -> str:
    """Subtitle tracks under names Plex and Jellyfin read: NRK's 'nb-ttv' (everything said) ->
    '.nb.srt', 'nb-nor' (only speech in other languages) -> '.nb.forced.srt', anything else ->
    its language code."""
    code, _, variant = lang.partition("-")
    flag = ".forced" if variant == "nor" else ""
    return f"{basename}.{code or 'nb'}{flag}.srt"
