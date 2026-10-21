from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class PlexConfig:
    url: str
    token: str
    tv_section: int | None = None
    movie_section: int | None = None


@dataclass
class JellyfinConfig:
    url: str
    api_key: str
    # Libraries to scan after a download, by name; empty = scan every library
    libraries: tuple[str, ...] = ()


@dataclass
class Config:
    library_dir: Path  # series
    movies_dir: Path  # films and single programs
    data_dir: Path
    interval_minutes: int = 60
    # Throttling: max download speed (yt-dlp syntax, e.g. "8M" = 8 MiB/s) and a random pause between videos
    rate_limit: str | None = None
    pause_seconds: tuple[int, int] = (0, 0)
    max_downloads_per_run: int = 0  # 0 = no limit
    heartbeat_url: str | None = None
    web_port: int | None = 8000  # None to disable the web page
    plex: PlexConfig | None = None
    jellyfin: JellyfinConfig | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nrktv.db"


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text()) or {}
    plex = raw.pop("plex", None)
    jellyfin = raw.pop("jellyfin", None)
    for key in ("library_dir", "movies_dir", "data_dir"):
        raw[key] = Path(raw[key])
    pause = raw.pop("pause_seconds", None)
    if isinstance(pause, (int, float)):
        pause = (pause, pause)
    if jellyfin:
        jellyfin["libraries"] = tuple(jellyfin.get("libraries") or ())
    return Config(
        **raw,
        pause_seconds=tuple(pause) if pause else (0, 0),
        plex=PlexConfig(**plex) if plex and plex.get("token") else None,
        jellyfin=JellyfinConfig(**jellyfin) if jellyfin and jellyfin.get("api_key") else None,
    )
