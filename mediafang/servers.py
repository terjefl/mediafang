"""Tell Plex and Jellyfin about new and removed files, and Uptime Kuma that we're alive.

A changed folder is scanned in whichever library holds it, found by the libraries' own
folders, so new YouTube libraries need no configuration here.
"""

import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


# ---- Plex ----------------------------------------------------------------------


def plex_call(cfg: Config, path: str, method: str = "GET", params: dict | list | None = None) -> dict | None:
    plex = cfg.plex
    query = urllib.parse.urlencode([*(params.items() if isinstance(params, dict) else params or []),
                                    ("X-Plex-Token", plex.token)])
    req = urllib.request.Request(
        f"{plex.url.rstrip('/')}{path}?{query}", method=method, headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
    return json.loads(body).get("MediaContainer") if body else None


def plex_sections(cfg: Config) -> list[dict]:
    """[{key, title, type, agent, scanner, language, locations: [...]}]"""
    result = []
    for d in (plex_call(cfg, "/library/sections") or {}).get("Directory", []):
        result.append({
            "key": d["key"], "title": d["title"], "type": d["type"], "agent": d.get("agent"),
            "scanner": d.get("scanner"), "language": d.get("language"),
            "locations": [loc["path"] for loc in d.get("Location", [])],
        })
    return result


def _holding(locations: list[str], path: str) -> bool:
    return any(path == loc or path.startswith(loc.rstrip("/") + "/") for loc in locations)


def refresh_plex(cfg: Config, folders: list[Path]) -> None:
    if not cfg.plex:
        return
    try:
        sections = plex_sections(cfg)
    except Exception as e:
        log.warning("Plex: could not list libraries: %s", e)
        return
    for folder in {f for f in folders}:
        path = cfg.server_path(folder)
        if not path:
            continue
        for s in sections:
            if _holding(s["locations"], path):
                try:
                    plex_call(cfg, f"/library/sections/{s['key']}/refresh", params={"path": path})
                    log.info("Plex: scanning %s in %s", path, s["title"])
                except Exception as e:
                    log.warning("Plex: scan of %s failed: %s", path, e)


# ---- Jellyfin ------------------------------------------------------------------


def jellyfin_call(cfg: Config, path: str, method: str = "GET", body=None) -> dict | list | None:
    jf = cfg.jellyfin
    req = urllib.request.Request(
        f"{jf.url.rstrip('/')}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f'MediaBrowser Token="{jf.api_key}"', "Accept": "application/json",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return json.loads(data) if data else None


def refresh_jellyfin(cfg: Config, folders: list[Path]) -> None:
    """Report the changed folders; Jellyfin scans them in whichever library holds them."""
    if not cfg.jellyfin:
        return
    paths = sorted({p for f in folders if (p := cfg.server_path(f))})
    if not paths:
        return
    try:
        jellyfin_call(cfg, "/Library/Media/Updated", "POST",
                      {"Updates": [{"Path": p, "UpdateType": "Modified"} for p in paths]})
        log.info("Jellyfin: reported %d changed folder(s)", len(paths))
    except Exception as e:
        log.warning("Jellyfin refresh failed: %s", e)


def refresh(cfg: Config, folders: list[Path]) -> None:
    refresh_plex(cfg, folders)
    refresh_jellyfin(cfg, folders)


def heartbeat(url: str | None, ok: bool, msg: str) -> None:
    """Uptime Kuma push-style heartbeat: ?status=up|down&msg=..."""
    if not url:
        return
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}{urllib.parse.urlencode({'status': 'up' if ok else 'down', 'msg': msg[:200]})}"
    try:
        urllib.request.urlopen(full, timeout=15).read()
    except Exception as e:
        log.warning("Heartbeat failed: %s", e)
