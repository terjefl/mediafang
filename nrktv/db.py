"""What we follow and every episode we know of, in SQLite.

The web page and the download loop run in separate threads, so every call opens its own
connection; SQLite in WAL mode handles the rest.
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS follows (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- series | program
    ref TEXT NOT NULL UNIQUE,        -- series slug or program id
    title TEXT NOT NULL,
    plot TEXT NOT NULL DEFAULT '',
    series_type TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'all', -- all | seasons | new
    seasons TEXT NOT NULL DEFAULT '[]',
    keep_days INTEGER,
    since TEXT NOT NULL,             -- when we started following; mode 'new' takes what comes after
    enabled INTEGER NOT NULL DEFAULT 1,
    image TEXT,
    poster TEXT,
    backdrop TEXT
);
CREATE TABLE IF NOT EXISTS seasons (
    follow_id INTEGER NOT NULL REFERENCES follows(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    scanned REAL NOT NULL,
    PRIMARY KEY (follow_id, name)
);
CREATE TABLE IF NOT EXISTS episodes (
    prf_id TEXT PRIMARY KEY,
    follow_id INTEGER NOT NULL REFERENCES follows(id) ON DELETE CASCADE,
    season INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    title TEXT NOT NULL,
    plot TEXT NOT NULL DEFAULT '',
    aired TEXT,
    expires TEXT,
    image TEXT,
    year INTEGER,
    original_title TEXT,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | done | failed | expired | geo | removed
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    retry_after REAL NOT NULL DEFAULT 0,
    files TEXT NOT NULL DEFAULT '[]',
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS episodes_follow ON episodes(follow_id, season, episode);
"""

MAX_ATTEMPTS = 5


@dataclass
class Follow:
    id: int
    kind: str
    ref: str
    title: str
    plot: str
    series_type: str
    mode: str
    seasons: list[str]
    keep_days: int | None
    since: datetime
    enabled: bool
    image: str | None
    poster: str | None
    backdrop: str | None

    @property
    def dated(self) -> bool:
        return self.kind == "series" and self.series_type != "sequential"

    def wants_season(self, name: str) -> bool:
        """For mode 'seasons': a chosen season, or for date series a month in a chosen year."""
        return self.mode != "seasons" or any(name == s or name.startswith(s) for s in self.seasons)


@dataclass
class EpisodeRow:
    prf_id: str
    follow_id: int
    season: int
    episode: int
    title: str
    plot: str
    aired: date | None
    expires: date | None
    image: str | None
    year: int | None
    original_title: str | None
    status: str
    attempts: int
    error: str | None
    retry_after: float
    files: list[str] = field(default_factory=list)
    updated: float = 0.0


def _follow(r: sqlite3.Row) -> Follow:
    return Follow(
        id=r["id"],
        kind=r["kind"],
        ref=r["ref"],
        title=r["title"],
        plot=r["plot"],
        series_type=r["series_type"],
        mode=r["mode"],
        seasons=json.loads(r["seasons"]),
        keep_days=r["keep_days"],
        since=datetime.fromisoformat(r["since"]),
        enabled=bool(r["enabled"]),
        image=r["image"],
        poster=r["poster"],
        backdrop=r["backdrop"],
    )


def _episode(r: sqlite3.Row) -> EpisodeRow:
    return EpisodeRow(
        prf_id=r["prf_id"],
        follow_id=r["follow_id"],
        season=r["season"],
        episode=r["episode"],
        title=r["title"],
        plot=r["plot"],
        aired=date.fromisoformat(r["aired"]) if r["aired"] else None,
        expires=date.fromisoformat(r["expires"]) if r["expires"] else None,
        image=r["image"],
        year=r["year"],
        original_title=r["original_title"],
        status=r["status"],
        attempts=r["attempts"],
        error=r["error"],
        retry_after=r["retry_after"],
        files=json.loads(r["files"]),
        updated=r["updated"],
    )


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    # ---- follows -------------------------------------------------------------

    def follows(self, enabled_only: bool = False) -> list[Follow]:
        sql = "SELECT * FROM follows" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY title COLLATE NOCASE"
        with self.conn() as c:
            return [_follow(r) for r in c.execute(sql)]

    def follow(self, follow_id: int) -> Follow | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM follows WHERE id = ?", (follow_id,)).fetchone()
        return _follow(r) if r else None

    def follow_by_ref(self, ref: str) -> Follow | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM follows WHERE ref = ?", (ref,)).fetchone()
        return _follow(r) if r else None

    def save_follow(
        self,
        kind: str,
        ref: str,
        title: str,
        mode: str = "all",
        seasons: list[str] | None = None,
        keep_days: int | None = None,
        **meta,
    ) -> Follow:
        """Start following, or change how an existing follow works (its episodes stay)."""
        seasons_json = json.dumps(seasons or [])
        with self.conn() as c:
            c.execute(
                """INSERT INTO follows (kind, ref, title, mode, seasons, keep_days, since)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ref) DO UPDATE SET mode = excluded.mode, seasons = excluded.seasons,
                       keep_days = excluded.keep_days, enabled = 1""",
                (kind, ref, title, mode, seasons_json, keep_days, datetime.now().astimezone().isoformat()),
            )
            # The choice of seasons decides what gets scanned: look at every season again
            fid = c.execute("SELECT id FROM follows WHERE ref = ?", (ref,)).fetchone()["id"]
            c.execute("DELETE FROM seasons WHERE follow_id = ?", (fid,))
        if meta:
            self.update_meta(fid, title=title, **meta)
        return self.follow(fid)

    def update_meta(self, follow_id: int, **fields) -> None:
        allowed = {"title", "plot", "series_type", "image", "poster", "backdrop"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        with self.conn() as c:
            sets = ", ".join(f"{k} = ?" for k in fields)
            c.execute(f"UPDATE follows SET {sets} WHERE id = ?", (*fields.values(), follow_id))

    def set_enabled(self, follow_id: int, enabled: bool) -> None:
        with self.conn() as c:
            c.execute("UPDATE follows SET enabled = ? WHERE id = ?", (int(enabled), follow_id))

    def delete_follow(self, follow_id: int) -> None:
        """Stop following. Downloaded files stay in the library."""
        with self.conn() as c:
            c.execute("DELETE FROM follows WHERE id = ?", (follow_id,))

    # ---- seasons -------------------------------------------------------------

    def scanned(self, follow_id: int) -> dict[str, float]:
        with self.conn() as c:
            rows = c.execute("SELECT name, scanned FROM seasons WHERE follow_id = ?", (follow_id,))
            return {r["name"]: r["scanned"] for r in rows}

    def mark_scanned(self, follow_id: int, name: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO seasons (follow_id, name, scanned) VALUES (?, ?, ?) "
                "ON CONFLICT DO UPDATE SET scanned = excluded.scanned",
                (follow_id, name, time.time()),
            )

    # ---- episodes ------------------------------------------------------------

    def add_episode(self, ep: EpisodeRow, unique_number: bool = False) -> bool:
        """Record a newly found episode; False if we knew it. Season/episode numbers of known
        episodes never change: their files are named by them. With unique_number, an episode
        whose number is taken in its season gets the next free one (two broadcasts in a day)."""
        with self.conn() as c:
            if c.execute("SELECT 1 FROM episodes WHERE prf_id = ?", (ep.prf_id,)).fetchone():
                # Rights can be extended; keep the latest end date
                c.execute("UPDATE episodes SET expires = ? WHERE prf_id = ?", (_iso(ep.expires), ep.prf_id))
                return False
            number = ep.episode
            if unique_number:
                taken = {
                    r[0]
                    for r in c.execute(
                        "SELECT episode FROM episodes WHERE follow_id = ? AND season = ?", (ep.follow_id, ep.season)
                    )
                }
                while number in taken:
                    number += 1
            c.execute(
                """INSERT INTO episodes (prf_id, follow_id, season, episode, title, plot, aired, expires, image,
                       year, original_title, status, updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ep.prf_id, ep.follow_id, ep.season, number, ep.title, ep.plot, _iso(ep.aired),
                    _iso(ep.expires), ep.image, ep.year, ep.original_title, ep.status, time.time(),
                ),
            )
            return True

    def episodes(self, follow_id: int) -> list[EpisodeRow]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM episodes WHERE follow_id = ? ORDER BY season DESC, episode DESC", (follow_id,)
            )
            return [_episode(r) for r in rows]

    def episode(self, prf_id: str) -> EpisodeRow | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM episodes WHERE prf_id = ?", (prf_id,)).fetchone()
        return _episode(r) if r else None

    def due(self) -> list[EpisodeRow]:
        """Episodes to download now, those whose rights end first first."""
        with self.conn() as c:
            rows = c.execute(
                """SELECT e.* FROM episodes e JOIN follows f ON f.id = e.follow_id
                   WHERE f.enabled = 1 AND ((e.status = 'pending' AND e.retry_after <= ?)
                       OR (e.status = 'failed' AND e.attempts < ? AND e.retry_after <= ?))
                   ORDER BY e.expires IS NULL, e.expires, e.aired, e.season, e.episode""",
                (time.time(), MAX_ATTEMPTS, time.time()),
            )
            return [_episode(r) for r in rows]

    def set_status(self, prf_id: str, status: str, error: str | None = None, files: list[str] | None = None) -> None:
        with self.conn() as c:
            if status == "failed":
                attempts = c.execute("SELECT attempts FROM episodes WHERE prf_id = ?", (prf_id,)).fetchone()[0] + 1
                # an hour, then 2, 4, 8 ...
                retry_after = time.time() + 3600 * 2 ** (attempts - 1)
                c.execute(
                    "UPDATE episodes SET status = ?, error = ?, attempts = ?, retry_after = ?, updated = ? WHERE prf_id = ?",
                    (status, (error or "")[-500:], attempts, retry_after, time.time(), prf_id),
                )
            else:
                c.execute(
                    "UPDATE episodes SET status = ?, error = ?, updated = ?" + (", files = ?" if files is not None else "")
                    + " WHERE prf_id = ?",
                    (status, error, time.time(), *([json.dumps(files)] if files is not None else []), prf_id),
                )

    def postpone(self, prf_id: str, until: float) -> None:
        """Not out at NRK yet: try again later, without an error or a used-up attempt."""
        with self.conn() as c:
            c.execute(
                "UPDATE episodes SET status = 'pending', error = NULL, retry_after = ?, updated = ? WHERE prf_id = ?",
                (until, time.time(), prf_id),
            )

    def retry(self, prf_id: str) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE episodes SET status = 'pending', attempts = 0, retry_after = 0, error = NULL, updated = ? "
                "WHERE prf_id = ?",
                (time.time(), prf_id),
            )

    def counts(self) -> dict[int, dict[str, int]]:
        """Episodes per status, per follow."""
        result: dict[int, dict[str, int]] = {}
        with self.conn() as c:
            for r in c.execute("SELECT follow_id, status, COUNT(*) n FROM episodes GROUP BY follow_id, status"):
                result.setdefault(r["follow_id"], {})[r["status"]] = r["n"]
        return result

    def next_expiry(self) -> dict[int, date]:
        """The first date an episode we still want disappears from NRK, per follow."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT follow_id, MIN(expires) d FROM episodes WHERE expires IS NOT NULL "
                "AND status IN ('pending', 'failed') GROUP BY follow_id"
            )
            return {r["follow_id"]: date.fromisoformat(r["d"]) for r in rows if r["d"]}

    def recent(self, limit: int = 100) -> list[tuple[EpisodeRow, str]]:
        """Latest changes, with the title of the follow."""
        with self.conn() as c:
            rows = c.execute(
                """SELECT e.*, f.title AS show FROM episodes e JOIN follows f ON f.id = e.follow_id
                   WHERE e.status != 'pending' ORDER BY e.updated DESC LIMIT ?""",
                (limit,),
            )
            return [(_episode(r), r["show"]) for r in rows]

    def expired_done(self, follow_id: int, before: date) -> list[EpisodeRow]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM episodes WHERE follow_id = ? AND status = 'done' AND aired < ?",
                (follow_id, before.isoformat()),
            )
            return [_episode(r) for r in rows]


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None
