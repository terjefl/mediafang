from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class PlexConfig:
    url: str
    token: str
    # Kept for old configs; libraries are now found by their folders
    tv_section: int | None = None
    movie_section: int | None = None


@dataclass
class JellyfinConfig:
    url: str
    api_key: str
    # Kept for old configs; changed folders are reported to Jellyfin, which finds the library
    libraries: tuple[str, ...] = ()


@dataclass
class Config:
    library_dir: Path  # NRK series
    movies_dir: Path  # NRK films and single programs
    data_dir: Path
    youtube_dir: Path | None = None  # YouTube: <youtube_dir>/<library>/<channel>/...
    # The media share as this container sees it, and as Plex and Jellyfin see it: a folder
    # we change is scanned in whichever library holds it
    media_root: Path | None = None
    server_root: str = "/Media01"
    cookies_file: Path | None = None  # YouTube cookies.txt, for sources that use cookies
    pot_provider_url: str | None = None  # bgutil PO token provider, e.g. http://bgutil:4416
    pinchflat_db: Path | None = None  # for `migrate-pinchflat`
    pinchflat_root: str = "/downloads"  # Pinchflat's own path for the files it lists
    # Library folder per Pinchflat source (by its folder name); default TV or Video by layout
    pinchflat_libraries: dict[str, str] = field(default_factory=dict)
    interval_minutes: int = 60
    # Throttling: max download speed (yt-dlp syntax, e.g. "8M" = 8 MiB/s) and a random pause between videos
    rate_limit: str | None = None
    pause_seconds: tuple[int, int] = (0, 0)
    youtube_pause_seconds: tuple[int, int] = (5, 20)
    max_downloads_per_run: int = 0  # 0 = no limit
    heartbeat_url: str | None = None
    web_port: int | None = 8000  # None to disable the web page
    plex: PlexConfig | None = None
    jellyfin: JellyfinConfig | None = None

    @property
    def db_path(self) -> Path:
        path = self.data_dir / "mediafang.db"
        old = self.data_dir / "nrktv.db"
        if old.exists() and not path.exists():  # from before the rename
            for suffix in ("", "-wal", "-shm"):
                if (o := Path(str(old) + suffix)).exists():
                    o.rename(str(path) + suffix)
        return path

    def server_path(self, path: Path) -> str | None:
        """A path in this container as Plex and Jellyfin see it, if it is on the media share."""
        if not self.media_root:
            return None
        try:
            rest = path.relative_to(self.media_root)
        except ValueError:
            return None
        return f"{self.server_root.rstrip('/')}/{rest.as_posix()}".rstrip("/")


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text()) or {}
    plex = raw.pop("plex", None)
    jellyfin = raw.pop("jellyfin", None)
    for key in ("library_dir", "movies_dir", "data_dir"):
        raw[key] = Path(raw[key])
    for key in ("youtube_dir", "media_root", "cookies_file", "pinchflat_db"):
        if raw.get(key):
            raw[key] = Path(raw[key])
    for key in ("pause_seconds", "youtube_pause_seconds"):
        if isinstance(raw.get(key), (int, float)):
            raw[key] = (raw[key], raw[key])
        if raw.get(key):
            raw[key] = tuple(raw[key])
    pause = raw.pop("pause_seconds", None)
    if jellyfin:
        jellyfin["libraries"] = tuple(jellyfin.get("libraries") or ())
    return Config(
        **raw,
        pause_seconds=tuple(pause) if pause else (0, 0),
        plex=PlexConfig(**plex) if plex and plex.get("token") else None,
        jellyfin=JellyfinConfig(**jellyfin) if jellyfin and jellyfin.get("api_key") else None,
    )
