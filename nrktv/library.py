"""Library layout and Kodi-style NFO sidecars, which both Plex (its NFO agents) and Jellyfin read.

    <library_dir>/Fleksnes/tvshow.nfo, poster.jpg, fanart.jpg
    <library_dir>/Fleksnes/Season 01/Fleksnes - S01E01 - Fysiske fordeler.mp4 | .nfo | .nb.srt | .jpg
    <library_dir>/Dagsrevyen/Season 2026/Dagsrevyen - S2026E102201 - 2026-10-22.mp4
    <movies_dir>/Kongehusets hemmelige millioner (2024)/Kongehusets hemmelige millioner (2024).mp4 | .nfo
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import EpisodeRow, Follow
from .titles import date_title_suffix, safe_filename


@dataclass
class Target:
    root: Path  # show folder, or the film's own folder
    folder: Path  # where the video goes
    basename: str  # file name without extension

    @property
    def nfo(self) -> Path:
        return self.folder / f"{self.basename}.nfo"


def show_folder(cfg: Config, follow: Follow) -> Path:
    return cfg.library_dir / safe_filename(follow.title)


def season_dir(season: int) -> str:
    return f"Season {season:02d}" if season < 1000 else f"Season {season}"


def target(cfg: Config, follow: Follow, ep: EpisodeRow) -> Target:
    if follow.kind == "program":
        year = ep.year or (ep.aired.year if ep.aired else None)
        name = safe_filename(f"{ep.title} ({year})" if year else ep.title)
        root = cfg.movies_dir / name
        return Target(root, root, name)
    show = safe_filename(follow.title)
    root = cfg.library_dir / show
    if follow.dated:
        name = f"{show} - S{ep.season}E{ep.episode:06d} - {ep.aired.isoformat() if ep.aired else ep.title}"
        if extra := date_title_suffix(ep.title):
            name += f" - {safe_filename(extra, 80)}"
    else:
        name = f"{show} - S{ep.season:02d}E{ep.episode:02d}"
        if ep.title:
            name += f" - {safe_filename(ep.title, 100)}"
    return Target(root, root / season_dir(ep.season), safe_filename(name, 200))


def write_episode_nfo(path: Path, follow: Follow, ep: EpisodeRow) -> None:
    root = ET.Element("episodedetails")
    ET.SubElement(root, "title").text = ep.title or f"Episode {ep.episode}"
    ET.SubElement(root, "showtitle").text = follow.title
    ET.SubElement(root, "season").text = str(ep.season)
    ET.SubElement(root, "episode").text = str(ep.episode)
    if ep.aired:
        # A bare date: Plex reads a date with a time as no date at all
        ET.SubElement(root, "aired").text = ep.aired.isoformat()
        ET.SubElement(root, "premiered").text = ep.aired.isoformat()
    ET.SubElement(root, "plot").text = ep.plot
    ET.SubElement(root, "uniqueid", type="nrk", default="true").text = ep.prf_id
    _write_xml(root, path)


def write_show_nfo(path: Path, follow: Follow) -> None:
    root = ET.Element("tvshow")
    ET.SubElement(root, "title").text = follow.title
    ET.SubElement(root, "plot").text = follow.plot
    ET.SubElement(root, "studio").text = "NRK"
    ET.SubElement(root, "uniqueid", type="nrk", default="true").text = follow.ref
    _write_xml(root, path)


def write_movie_nfo(path: Path, ep: EpisodeRow) -> None:
    root = ET.Element("movie")
    ET.SubElement(root, "title").text = ep.title
    if ep.original_title and ep.original_title != ep.title:
        ET.SubElement(root, "originaltitle").text = ep.original_title
    if ep.year:
        ET.SubElement(root, "year").text = str(ep.year)
    if ep.aired:
        ET.SubElement(root, "premiered").text = ep.aired.isoformat()
    ET.SubElement(root, "plot").text = ep.plot
    ET.SubElement(root, "studio").text = "NRK"
    ET.SubElement(root, "uniqueid", type="nrk", default="true").text = ep.prf_id
    _write_xml(root, path)


def _write_xml(root: ET.Element, path: Path) -> None:
    ET.indent(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def subtitle_name(basename: str, lang: str) -> str:
    """NRK's subtitle tracks under names Plex and Jellyfin read: 'nb-ttv' (everything said) ->
    '.nb.srt', 'nb-nor' (only speech in other languages) -> '.nb.forced.srt', anything else ->
    its language code."""
    code, _, variant = lang.partition("-")
    flag = ".forced" if variant == "nor" else ""
    return f"{basename}.{code or 'nb'}{flag}.srt"
