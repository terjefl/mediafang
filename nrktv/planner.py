"""What to fetch for a follow, and where each NRK episode goes in the library.

Sequential series keep NRK's season and episode numbers. Date series (talk shows, news) have
none, so the season is the year and the episode number is made from the date the way Pinchflat
does for YouTube: MMDD plus a two-digit counter, so 22 October is E102201 and a second broadcast
that day E102202. The number follows from the date alone, whatever order episodes are found in.
"""

import time
from datetime import date, datetime, timedelta

from .db import EpisodeRow, Follow
from .psapi import Instalment, SeasonRef, Series
from .titles import strip_number

# Seasons we have looked at are looked at again after this long (new seasons and the newest
# season are looked at every run)
RESCAN_SECONDS = 7 * 24 * 3600


def newest_seasons(series: Series) -> list[SeasonRef]:
    """The seasons new episodes turn up in: the latest one, for date series the latest two
    (a new month or year starts while the last one may still get its final broadcast)."""
    if not series.seasons:
        return []
    if series.dated:
        return sorted(series.seasons, key=lambda s: s.name, reverse=True)[:2]
    numbered = [s for s in series.seasons if s.name.isdigit()]
    return [max(numbered, key=lambda s: int(s.name))] if numbered else series.seasons[-1:]


def seasons_to_scan(follow: Follow, series: Series, scanned: dict[str, float], now: float | None = None) -> list[SeasonRef]:
    now = now or time.time()
    newest = {s.name for s in newest_seasons(series)}
    if follow.mode == "new":
        refs = [s for s in series.seasons if s.name in newest]
    else:
        refs = [s for s in series.seasons if follow.wants_season(s.name)]
    if follow.dated and follow.keep_days:
        cutoff = date.today() - timedelta(days=follow.keep_days)
        refs = [s for s in refs if not _period_ends_before(s.name, cutoff)]
    return [s for s in refs if s.name in newest or s.name not in scanned or now - scanned[s.name] > RESCAN_SECONDS]


def _period_ends_before(name: str, cutoff: date) -> bool:
    """Whether a date series' season ("2024" or "202610") is over before cutoff."""
    if not name.isdigit():
        return False
    if len(name) == 4:
        return date(int(name), 12, 31) < cutoff
    if len(name) == 6:
        year, month = int(name[:4]), int(name[4:])
        if not 1 <= month <= 12:
            return False
        first_next = date(year + month // 12, month % 12 + 1, 1)
        return first_next - timedelta(days=1) < cutoff
    return False


def wanted(follow: Follow, inst: Instalment) -> bool:
    """Whether an episode NRK lists should be downloaded."""
    if not inst.available:
        return False  # not out yet, or gone
    if inst.rights_from and inst.rights_from > datetime.now(inst.rights_from.tzinfo):
        return False  # tonight's broadcast: listed before it can be watched; a later run takes it
    if follow.mode == "new":
        released = inst.released.timestamp() if inst.released else None
        if released is None or released < follow.since.timestamp():
            return False
    if follow.dated and follow.keep_days and inst.aired:
        if inst.aired < date.today() - timedelta(days=follow.keep_days):
            return False
    return True


def season_number(series: Series, ref: SeasonRef) -> int:
    if ref.name.isdigit() and not series.dated:
        return int(ref.name)
    names = [s.name for s in series.seasons]
    return names.index(ref.name) + 1 if ref.name in names else 1


def plan(follow: Follow, series: Series, ref: SeasonRef, position: int, inst: Instalment) -> EpisodeRow:
    """The library row for an episode. position: its 0-based place in the season's list."""
    if series.dated:
        aired = inst.aired or (inst.released.date() if inst.released else date.today())
        season = aired.year
        episode = aired.month * 10000 + aired.day * 100 + 1
        title = inst.title
    else:
        aired = inst.aired
        season = season_number(series, ref)
        episode = inst.sequence or position + 1
        title = strip_number(inst.title)
    return EpisodeRow(
        prf_id=inst.prf_id,
        follow_id=follow.id,
        season=season,
        episode=episode,
        title=title,
        plot=inst.plot,
        aired=aired,
        expires=inst.expires,
        image=inst.image,
        year=inst.year,
        original_title=inst.original_title,
        status="pending",
        attempts=0,
        error=None,
        retry_after=0,
    )
