"""Tell Plex and Jellyfin to scan for new files, and Uptime Kuma that we're alive."""

import json
import logging
import urllib.parse
import urllib.request

from .config import Config

log = logging.getLogger(__name__)


def refresh_plex(cfg: Config, tv: bool = True, movies: bool = True) -> None:
    plex = cfg.plex
    if not plex:
        return
    sections = [s for s, want in ((plex.tv_section, tv), (plex.movie_section, movies)) if s and want]
    for section in sections:
        query = urllib.parse.urlencode({"X-Plex-Token": plex.token})
        try:
            urllib.request.urlopen(f"{plex.url.rstrip('/')}/library/sections/{section}/refresh?{query}", timeout=30).read()
            log.info("Triggered Plex scan of section %s", section)
        except Exception as e:
            log.warning("Plex refresh of section %s failed: %s", section, e)


def _jellyfin(cfg: Config, path: str, method: str = "GET") -> dict | None:
    jf = cfg.jellyfin
    req = urllib.request.Request(
        f"{jf.url.rstrip('/')}{path}",
        method=method,
        headers={"Authorization": f'MediaBrowser Token="{jf.api_key}"', "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    return json.loads(body) if body else None


def refresh_jellyfin(cfg: Config) -> None:
    """Scan the configured libraries (all of them if none are named)."""
    jf = cfg.jellyfin
    if not jf:
        return
    try:
        if not jf.libraries:
            _jellyfin(cfg, "/Library/Refresh", "POST")
            log.info("Triggered Jellyfin library scan")
            return
        folders = {f["Name"]: f["ItemId"] for f in _jellyfin(cfg, "/Library/VirtualFolders") or []}
        for name in jf.libraries:
            if name not in folders:
                log.warning("Jellyfin has no library called %r", name)
                continue
            query = urllib.parse.urlencode(
                {"Recursive": "true", "MetadataRefreshMode": "Default", "ImageRefreshMode": "Default"}
            )
            _jellyfin(cfg, f"/Items/{folders[name]}/Refresh?{query}", "POST")
            log.info("Triggered Jellyfin scan of %s", name)
    except Exception as e:
        log.warning("Jellyfin refresh failed: %s", e)


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
