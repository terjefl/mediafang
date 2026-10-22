"""Take over Pinchflat: its sources and downloaded videos into our DB, and its folders from
<media_root>/Pinchflat to <youtube_dir>/<library>/<channel>.

Nothing is downloaded again and nothing is deleted. Each source's folder is moved with one
rename on the media share (so no data is copied, and it fails rather than copies if source and
target were on different file systems). Plex and Jellyfin get the new folder added to the
library that held the old one before anything moves, so they see moved files, not new ones,
and keep watched status. Removing the old folder from the libraries is a separate step
(--finish), once their scans are done.

    mediafang migrate-pinchflat            show what would happen
    mediafang migrate-pinchflat --apply    import, add library folders, move
    mediafang migrate-pinchflat --finish   remove the old Pinchflat folders from the libraries
"""

import logging
import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import servers
from .config import Config
from .db import DB, Item, Source
from .library import source_root
from .providers import date_episode, youtube

log = logging.getLogger(__name__)

_SXE = re.compile(r"^s(\d{4})e(\d{6})\b", re.IGNORECASE)


@dataclass
class Plan:
    pinchflat_id: int
    title: str
    settings: dict
    kind: str
    ref: str
    old_root: Path  # as this container sees it
    new_root: Path
    items: list[Item] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def open_ro(path: Path) -> sqlite3.Connection:
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        c.execute("SELECT 1 FROM sources LIMIT 1")
    except sqlite3.OperationalError:  # read-only folder and no -shm: read it as a fixed file
        c = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _profile_settings(p: sqlite3.Row) -> dict:
    template = p["output_path_template"] or ""
    tv = "season_by_year" in template or "season_episode" in template or bool(p["download_nfo"])
    resolution = (p["preferred_resolution"] or "1080p").rstrip("p")
    return {
        "layout": "tv" if tv else "videos",
        "quality": 0 if resolution == "audio" else int(resolution) if resolution.isdigit() else 1080,
        "shorts": p["shorts_behaviour"] in ("include", "only"),
        "livestreams": p["livestream_behaviour"] != "exclude",
        "sponsorblock": {"disabled": ""}.get(p["sponsorblock_behaviour"], p["sponsorblock_behaviour"] or ""),
        "sb_categories": ",".join(_json_list(p["sponsorblock_categories"])) or "sponsor,selfpromo,interaction",
        "subtitles": (p["sub_langs"] or "") if p["download_subs"] else "",
    }


def _json_list(value) -> list[str]:
    import json

    try:
        return list(json.loads(value or "[]"))
    except (ValueError, TypeError):
        return []


def _local(cfg: Config, pinchflat_path: str | None) -> Path | None:
    """A path Pinchflat recorded (/downloads/...) as this container sees it."""
    if not pinchflat_path:
        return None
    root = cfg.pinchflat_root.rstrip("/") + "/"
    if not pinchflat_path.startswith(root):
        return None
    return cfg.media_root / "Pinchflat" / pinchflat_path[len(root):]


def plan(cfg: Config, db: DB) -> list[Plan]:
    if not (cfg.pinchflat_db and cfg.media_root and cfg.youtube_dir):
        raise SystemExit("Needs pinchflat_db, media_root and youtube_dir in the config")
    pf = open_ro(cfg.pinchflat_db)
    profiles = {p["id"]: _profile_settings(p) for p in pf.execute("SELECT * FROM media_profiles")}
    plans = []
    for s in pf.execute("SELECT * FROM sources ORDER BY id"):
        settings = dict(profiles[s["media_profile_id"]])
        folder = s["custom_name"] or s["collection_name"]
        settings.update(
            folder=folder,
            library=cfg.pinchflat_libraries.get(folder, "TV" if settings["layout"] == "tv" else "Video"),
            cookies=s["cookie_behaviour"] != "disabled",
            cutoff=date.fromisoformat(s["download_cutoff_date"][:10]) if s["download_cutoff_date"] else None,
            title_regex=s["title_filter_regex"] or "",
            min_duration=s["min_duration_seconds"],
            max_duration=s["max_duration_seconds"],
            keep_days=s["retention_period_days"] or None,
        )
        ref = s["collection_id"]
        kind = "channel" if ref.startswith("UC") else "playlist"
        settings["url"] = youtube.source_url(kind, ref)
        rows = pf.execute("SELECT * FROM media_items WHERE source_id = ? ORDER BY uploaded_at", (s["id"],)).fetchall()
        files = [_local(cfg, r["media_filepath"]) for r in rows if r["media_filepath"]]
        files = [f for f in files if f]
        # The source's folder: above "Season 2026/" (TV) or above the per-video folder (videos).
        # The most common one among files that exist: items can keep paths from an older
        # layout whose files are gone
        files = [f for f in files if f.exists()] or files
        old_root = Counter(f.parent.parent for f in files).most_common(1)[0][0] if files else None
        source = Source(id=0, provider="youtube", kind=kind, ref=ref, title=s["collection_name"],
                        **{k: v for k, v in settings.items() if k in Source.__dataclass_fields__})
        p = Plan(s["id"], s["collection_name"], settings, kind, ref, old_root, source_root(cfg, source))
        for r in rows:
            p.items.append(_item(cfg, r, settings["layout"], p))
        plans.append(p)
    return plans


def _item(cfg: Config, r: sqlite3.Row, layout: str, p: Plan) -> Item:
    uploaded = datetime.fromisoformat(r["uploaded_at"].replace("Z", "+00:00")).date() if r["uploaded_at"] else None
    media = _local(cfg, r["media_filepath"])
    season = uploaded.year if uploaded else 0
    episode = date_episode(uploaded, r["upload_date_index"] or 99) if uploaded else 0
    if media and (m := _SXE.match(media.name)):
        season, episode = int(m[1]), int(m[2])
    paths = [media, _local(cfg, r["thumbnail_filepath"]), _local(cfg, r["nfo_filepath"]),
             _local(cfg, r["metadata_filepath"])]
    paths += [_local(cfg, sub[1]) for sub in _json_list(r["subtitle_filepaths"]) if len(sub) > 1]
    if media and media.exists():
        status, error = "done", None
    elif media and r["prevent_download"]:
        status, error = "skipped", "fjernet i Pinchflat"
    elif media:
        status, error = "skipped", "fil mangler"
        p.missing.append(str(media))
    else:
        status, error = "skipped", "short" if r["short_form_content"] else "ikke lastet ned"
    files = [str(_moved(x, p)) for x in paths if x]
    return Item(
        provider="youtube", item_id=r["media_id"], source_id=0, season=season, episode=episode,
        title=r["title"] or r["media_id"], plot=r["description"] or "", aired=uploaded,
        duration=r["duration_seconds"], is_short=bool(r["short_form_content"]), is_live=bool(r["livestream"]),
        status=status, error=error, files=files if status == "done" else [], size=r["media_size_bytes"] or 0,
    )


def _moved(path: Path, p: Plan) -> Path:
    try:
        return p.new_root / path.relative_to(p.old_root)
    except (ValueError, TypeError):
        return path


def _library_moves(cfg: Config, p: Plan) -> list[tuple[str, str]]:
    """(old, new) library folders as Plex/Jellyfin see them: a library may hold the channel's
    folder itself (videos) or the folder above it (TV shows)."""
    old, new = cfg.server_path(p.old_root), cfg.server_path(p.new_root)
    old_up, new_up = cfg.server_path(p.old_root.parent), cfg.server_path(p.new_root.parent)
    return [(old, new), (old_up, new_up)]


def migrate(cfg: Config, db: DB, apply: bool = False, finish: bool = False) -> bool:
    plans = plan(cfg, db)
    # Each step can be run again: what is imported or moved already is left alone
    imported = {p.ref for p in plans if (s := db.source_by_ref("youtube", p.ref)) and db.items(s.id)}
    ok = True
    for p in plans:
        counts: dict[str, int] = {}
        for item in p.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        print(f"\n{p.title} ({p.settings['layout']}, {p.kind} {p.ref})")
        print(f"  {p.old_root}\n  -> {p.new_root}")
        print(f"  items: {counts}")
        for m in p.missing[:5]:
            print(f"  missing: {m}")
        if p.ref in imported:
            print("  imported already")
        if p.old_root is None:
            print("  (no files)")
        elif p.new_root.exists() and not p.old_root.exists():
            print("  moved already")
        elif p.new_root.exists() and not finish:
            print("  ! target folder exists already")
            ok = False
    if not cfg.pinchflat_db:
        return False
    if finish:
        return _finish(cfg, plans)
    if not apply:
        print("\nNothing changed. Run with --apply to import and move.")
        return ok
    if not ok:
        print("\nStopped: fix the problems above first.")
        return False

    for p in plans:
        if p.ref in imported:
            continue
        source = db.save_source("youtube", p.kind, p.ref, p.title, **p.settings)
        for item in p.items:
            item.source_id = source.id
            db.add_item(item)
        db.touch(source.id, full_index=True)
        print(f"{p.title}: {len(p.items)} items imported")

    _add_plex_folders(cfg, plans)
    for p in plans:
        if p.old_root is None or not p.old_root.exists():
            continue
        p.new_root.parent.mkdir(parents=True, exist_ok=True)
        if os.stat(p.old_root).st_dev != os.stat(p.new_root.parent).st_dev:
            print(f"{p.title}: NOT moved, {p.old_root} and {p.new_root.parent} are on different file systems")
            ok = False
            continue
        os.rename(p.old_root, p.new_root)
        print(f"{p.title}: moved to {p.new_root}")
    # Jellyfin only takes folders that exist, so its new folders come right after the move
    _add_jellyfin_folders(cfg, plans)
    servers.refresh(cfg, [p.new_root for p in plans])
    print("\nWhen Plex and Jellyfin have scanned, check the libraries and run with --finish.")
    return ok


def _add_plex_folders(cfg: Config, plans: list[Plan]) -> None:
    moves = [m for p in plans if p.old_root for m in _library_moves(cfg, p)]
    if cfg.plex:
        for section in servers.plex_sections(cfg):
            locations = list(section["locations"])
            added = [new for old, new in moves if old in locations and new not in locations]
            if added:
                set_plex_locations(cfg, section, locations + added)
                print(f"Plex «{section['title']}»: + {', '.join(added)}")


def _add_jellyfin_folders(cfg: Config, plans: list[Plan]) -> None:
    moves = [m for p in plans if p.old_root for m in _library_moves(cfg, p)]
    if cfg.jellyfin:
        for folder in servers.jellyfin_call(cfg, "/Library/VirtualFolders") or []:
            for old, new in moves:
                if old in folder.get("Locations", []) and new not in folder["Locations"]:
                    servers.jellyfin_call(cfg, "/Library/VirtualFolders/Paths?refreshLibrary=false", "POST",
                                          {"Name": folder["Name"], "PathInfo": {"Path": new}})
                    print(f"Jellyfin «{folder['Name']}»: + {new}")


def _finish(cfg: Config, plans: list[Plan]) -> bool:
    moves = [m for p in plans if p.old_root for m in _library_moves(cfg, p)]
    olds = {old for old, _ in moves}
    if cfg.plex:
        for section in servers.plex_sections(cfg):
            keep = [loc for loc in section["locations"] if loc not in olds]
            if keep != section["locations"] and keep:
                set_plex_locations(cfg, section, keep)
                print(f"Plex «{section['title']}»: - {', '.join(set(section['locations']) - set(keep))}")
    if cfg.jellyfin:
        from urllib.parse import urlencode

        for folder in servers.jellyfin_call(cfg, "/Library/VirtualFolders") or []:
            for loc in folder.get("Locations", []):
                if loc in olds and len(folder["Locations"]) > 1:
                    q = urlencode({"name": folder["Name"], "path": loc, "refreshLibrary": "false"})
                    servers.jellyfin_call(cfg, f"/Library/VirtualFolders/Paths?{q}", "DELETE")
                    print(f"Jellyfin «{folder['Name']}»: - {loc}")
    print("Done. The empty Pinchflat folders are yours to delete.")
    return True


def set_plex_locations(cfg: Config, section: dict, locations: list[str]) -> None:
    params = [("name", section["title"]), ("agent", section["agent"]), ("scanner", section["scanner"]),
              ("language", section["language"])]
    params += [("location", loc) for loc in locations]
    servers.plex_call(cfg, f"/library/sections/{section['key']}", "PUT", params)
