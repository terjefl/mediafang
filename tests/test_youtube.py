import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from mediafang import feeds
from mediafang.config import Config
from mediafang.db import DB, Item
from mediafang.harvest import _failure_status
from mediafang.library import source_root, target, write_episode_nfo
from mediafang.providers import date_episode, parse_url, youtube

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg(tmp_path):
    return Config(
        library_dir=tmp_path / "media/NRK", movies_dir=tmp_path / "media/NRK Filmer", data_dir=tmp_path / "data",
        youtube_dir=tmp_path / "media/YouTube", media_root=tmp_path / "media",
    )


@pytest.fixture
def db(cfg):
    return DB(cfg.db_path)


def channel(db, **settings):
    return db.save_source("youtube", "channel", "UCdUXAN90UQV4sxNehj5Vflw", "Nora En Pure", **settings)


def test_parse_url():
    assert parse_url("https://www.youtube.com/@NoraEnPure") == ("youtube", "channel", "@NoraEnPure")
    assert parse_url("https://www.youtube.com/@NoraEnPure/videos") == ("youtube", "channel", "@NoraEnPure")
    assert parse_url("https://youtube.com/channel/UC0A_lGU0Hw_mA1SGvl34Gfg") == ("youtube", "channel", "UC0A_lGU0Hw_mA1SGvl34Gfg")
    assert parse_url("https://www.youtube.com/c/korolovadj") == ("youtube", "channel", "c/korolovadj")
    assert parse_url("https://www.youtube.com/playlist?list=PL6RLee9oArCC9V1FlRexG_6F2KNo143I8") == (
        "youtube", "playlist", "PL6RLee9oArCC9V1FlRexG_6F2KNo143I8")
    assert parse_url("https://tv.nrk.no/serie/fleksnes") == ("nrk", "series", "fleksnes")
    assert parse_url("https://www.youtube.com/watch?v=DkvzBZpdcU4") is None


def test_parse_feed():
    entries = youtube.parse_feed((FIXTURES / "youtube-rss.xml").read_bytes())
    assert entries and entries[0].video_id == "DkvzBZpdcU4"
    assert entries[0].title == "Purified Radio 530"
    assert entries[0].published == datetime(2026, 10, 19, 9, 0, 32, tzinfo=UTC)
    assert entries[0].image.endswith("hqdefault.jpg")


def test_date_numbering_follows_pinchflat():
    assert date_episode(date(2026, 10, 22), youtube.FIRST_INDEX) == 102299


def test_filters(db):
    s = channel(db, title_regex="purified radio", min_duration=1800, cutoff=date(2026, 1, 1))
    ok = dict(title="Purified Radio 530", published=date(2026, 10, 19), duration=3550, is_short=False, is_live=False)
    assert youtube.filter_reason(s, **ok) is None
    assert youtube.filter_reason(s, **{**ok, "title": "Live at Cercle"}) == "tittelfilter"
    assert youtube.filter_reason(s, **{**ok, "duration": 60}) == "for kort"
    assert youtube.filter_reason(s, **{**ok, "published": date(2025, 5, 1)}).startswith("før")
    assert youtube.filter_reason(s, **{**ok, "is_short": True}) == "short"
    s = channel(db, livestreams=False, title_regex="")
    assert youtube.filter_reason(s, **{**ok, "is_live": True}) == "livestream"


def test_scan_records_new_and_skipped(db, cfg, monkeypatch):
    s = channel(db, min_duration=600)
    now = datetime.now(UTC)
    flat = [
        youtube.Entry("aaaaaaaaaaa", "Set 1", now, 3600, False, False, None),
        youtube.Entry("bbbbbbbbbbb", "Teaser", now, 30, False, False, None),
    ]
    monkeypatch.setattr(youtube, "list_flat", lambda cfg, source, known: [e for e in flat if e.video_id not in known])
    monkeypatch.setattr(youtube, "list_feed", lambda source: [])
    assert youtube.scan(db, s, cfg) == 1
    assert db.item("youtube", "bbbbbbbbbbb").status == "skipped"
    assert db.item("youtube", "aaaaaaaaaaa").episode == date_episode(now.date(), 99)
    # the next run within a day reads the feed instead of listing
    called = []
    monkeypatch.setattr(youtube, "list_flat", lambda *a: called.append(1) or [])
    youtube.scan(db, db.source(s.id), cfg)
    assert not called


def test_same_day_counts_down(db, cfg, monkeypatch):
    s = channel(db)
    day = datetime(2026, 10, 22, 12, tzinfo=UTC)
    monkeypatch.setattr(youtube, "list_flat", lambda *a: [
        youtube.Entry("aaaaaaaaaaa", "A", day, 3600, False, False, None),
        youtube.Entry("bbbbbbbbbbb", "B", day, 3600, False, False, None),
    ])
    youtube.scan(db, s, cfg)
    assert sorted(i.episode for i in db.items(s.id)) == [102298, 102299]


def test_prepare(db):
    s = channel(db, max_duration=7200)
    item = Item("youtube", "aaaaaaaaaaa", s.id, 2026, 102299, "Set", aired=date(2026, 10, 20))
    d = youtube.prepare(s, item, {"upload_date": "20261019", "duration": 3600, "title": "Set", "live_status": "not_live"})
    assert d.action == "ok" and item.aired == date(2026, 10, 19) and item.episode == 101999
    assert youtube.prepare(s, item, {"live_status": "is_upcoming", "release_timestamp": time.time() + 600}).action == "wait"
    assert youtube.prepare(s, item, {"duration": 9000, "live_status": "not_live"}).action == "skip"
    short = {"duration": 40, "width": 1080, "height": 1920, "live_status": "not_live"}
    assert youtube.prepare(s, item, short).reason == "short"


def test_layouts(db, cfg):
    item = Item("youtube", "DkvzBZpdcU4", 0, 2026, 101999, "Purified Radio 530", aired=date(2026, 10, 19))
    tv = channel(db, layout="tv", library="Mandy", folder="Mandy Cane Lane - YouTube")
    t = target(cfg, tv, item)
    assert t.folder == cfg.youtube_dir / "Mandy/Mandy Cane Lane - YouTube/Season 2026"
    assert t.basename == "s2026e101999 - Purified Radio 530 [DkvzBZpdcU4]"
    videos = channel(db, layout="videos", library="", folder="")
    t = target(cfg, videos, item)
    assert t.folder == cfg.youtube_dir / "Video/Nora En Pure/2026-10-19 Purified Radio 530"
    assert t.basename == "Purified Radio 530 [DkvzBZpdcU4]"
    audio = channel(db, layout="audio")
    t = target(cfg, audio, item)
    assert t.folder == cfg.youtube_dir / "Lyd/Nora En Pure" and t.basename.startswith("2026-10-19 Purified")


def test_episode_nfo(db, cfg, tmp_path):
    s = channel(db, layout="tv", folder="Nora En Pure - YouTube")
    item = Item("youtube", "DkvzBZpdcU4", s.id, 2026, 101999, "Purified Radio 530", aired=date(2026, 10, 19), duration=3550)
    write_episode_nfo(tmp_path / "e.nfo", s, item)
    root = ET.parse(tmp_path / "e.nfo").getroot()
    assert root.findtext("showtitle") == "Nora En Pure - YouTube"
    assert root.findtext("aired") == "2026-10-19" and root.findtext("episode") == "101999"
    assert root.find("uniqueid").get("type") == "youtube"


def test_options(db, cfg):
    s = channel(db, quality=1080, sponsorblock="remove", sb_categories="sponsor,intro", subtitles="nb,en")
    o = youtube.ytdlp_options(cfg, s, None)
    assert o["format_sort"][0] == "res:1080" and o["subtitleslangs"] == ["nb", "en"]
    keys = [p["key"] for p in o["postprocessors"]]
    assert keys == ["SponsorBlock", "ModifyChapters"]
    assert o["postprocessors"][1]["remove_sponsor_segments"] == ["sponsor", "intro"]
    audio = youtube.ytdlp_options(cfg, channel(db, quality=0, sponsorblock=""), None)
    assert audio["format"].startswith("ba")


def test_youtube_errors():
    item = Item("youtube", "x", 0, 0, 0, "")
    assert _failure_status(item, Exception("ERROR: [youtube] x: Private video. Sign in if ...")) == "unavailable"
    assert _failure_status(item, Exception("Sign in to confirm you're not a bot")) == "throttled"
    assert _failure_status(item, Exception("Join this channel to get access to members-only content")) == "unavailable"
    assert _failure_status(item, Exception("HTTP Error 500")) == "failed"


def test_feed(db, cfg):
    s = channel(db, layout="audio", quality=0)
    folder = source_root(cfg, s)
    folder.mkdir(parents=True)
    f = folder / "2026-10-19 Purified Radio 530 [DkvzBZpdcU4].m4a"
    f.write_bytes(b"x" * 100)
    item = Item("youtube", "DkvzBZpdcU4", s.id, 2026, 101999, "Purified Radio 530", aired=date(2026, 10, 19),
                duration=3550, status="done", files=[str(f)])
    db.add_item(item)
    xml = feeds.render(db, s, "https://media.flagan.net")
    root = ET.fromstring(xml)
    enc = root.find("channel/item/enclosure")
    assert enc.get("url") == f"https://media.flagan.net/feed/{s.feed_token}/DkvzBZpdcU4.m4a"
    assert enc.get("length") == "100" and enc.get("type") == "audio/mp4"


# ---- migrations ----------------------------------------------------------------

V1 = """
CREATE TABLE follows (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
  plot TEXT NOT NULL DEFAULT '', series_type TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'all',
  seasons TEXT NOT NULL DEFAULT '[]', keep_days INTEGER, since TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  image TEXT, poster TEXT, backdrop TEXT);
CREATE TABLE seasons (follow_id INTEGER NOT NULL, name TEXT NOT NULL, scanned REAL NOT NULL, PRIMARY KEY (follow_id, name));
CREATE TABLE episodes (prf_id TEXT PRIMARY KEY, follow_id INTEGER NOT NULL, season INTEGER NOT NULL,
  episode INTEGER NOT NULL, title TEXT NOT NULL, plot TEXT NOT NULL DEFAULT '', aired TEXT, expires TEXT, image TEXT,
  year INTEGER, original_title TEXT, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT, retry_after REAL NOT NULL DEFAULT 0, files TEXT NOT NULL DEFAULT '[]', updated REAL NOT NULL);
INSERT INTO follows (id, kind, ref, title, series_type, since) VALUES (3, 'series', 'debatten', 'Debatten', 'news', '2026-10-21T22:00:00+02:00');
INSERT INTO follows (id, kind, ref, title, since) VALUES (4, 'program', 'KOID22008424', 'K', '2026-10-21T22:00:00+02:00');
INSERT INTO seasons VALUES (3, '202610', 1.0);
INSERT INTO episodes (prf_id, follow_id, season, episode, title, aired, status, files, updated)
  VALUES ('NNFA51102226', 3, 2026, 102201, 'I dag', '2026-10-22', 'done', '["/x.mp4"]', 1.0);
"""


def test_migrate_from_nrktv(tmp_path):
    (tmp_path / "data").mkdir()
    old = sqlite3.connect(tmp_path / "data/nrktv.db")
    old.executescript(V1)
    old.close()
    cfg = Config(library_dir=tmp_path / "NRK", movies_dir=tmp_path / "F", data_dir=tmp_path / "data")
    db = DB(cfg.db_path)
    assert cfg.db_path.name == "mediafang.db" and not (tmp_path / "data/nrktv.db").exists()
    s = db.source(3)
    assert (s.provider, s.layout, s.series_type, s.title) == ("nrk", "tv", "news", "Debatten")
    assert db.source(4).layout == "movie"
    item = db.item("nrk", "NNFA51102226")
    assert (item.status, item.episode, item.files) == ("done", 102201, ["/x.mp4"])
    assert db.scanned(3) == {"202610": 1.0}
    DB(cfg.db_path)  # a second start leaves it alone


PINCHFLAT = """
CREATE TABLE media_profiles (id INTEGER PRIMARY KEY, name TEXT, output_path_template TEXT, download_subs INT,
  download_auto_subs INT, embed_subs INT, sub_langs TEXT, download_thumbnail INT, embed_thumbnail INT,
  shorts_behaviour TEXT, livestream_behaviour TEXT, download_metadata INT, embed_metadata INT,
  preferred_resolution TEXT, download_nfo INT, download_source_images INT, sponsorblock_behaviour TEXT,
  sponsorblock_categories TEXT, redownload_delay_days INT, marked_for_deletion_at TEXT, media_container TEXT,
  audio_track TEXT);
CREATE TABLE sources (id INTEGER PRIMARY KEY, collection_name TEXT, collection_id TEXT, collection_type TEXT,
  original_url TEXT, media_profile_id INT, custom_name TEXT, cookie_behaviour TEXT, download_cutoff_date TEXT,
  title_filter_regex TEXT, min_duration_seconds INT, max_duration_seconds INT, retention_period_days INT);
CREATE TABLE media_items (id INTEGER PRIMARY KEY, media_id TEXT, title TEXT, media_filepath TEXT, source_id INT,
  subtitle_filepaths TEXT, thumbnail_filepath TEXT, metadata_filepath TEXT, livestream INT, description TEXT,
  media_size_bytes INT, short_form_content INT, uploaded_at TEXT, nfo_filepath TEXT, duration_seconds INT,
  prevent_download INT, upload_date_index INT);
INSERT INTO media_profiles VALUES (1, 'Plex', '/{{ source_custom_name }}/{{ upload_yyyy_mm_dd }} {{ title }}/{{ title }} [{{ id }}].{{ ext }}',
  0,0,0,'nb-NO',1,0,'exclude','include',1,1,'2160p',0,0,'disabled','[]',NULL,NULL,NULL,NULL);
INSERT INTO media_profiles VALUES (2, 'Plex TV (NFO)', '/TV/{{ source_custom_name }}/{{ source_custom_name }}/{{ season_by_year__episode_by_date_and_index }} - {{ title }} [{{ id }}].{{ ext }}',
  0,0,0,'nb-NO',1,0,'exclude','include',1,1,'2160p',1,1,'disabled','[]',NULL,NULL,NULL,NULL);
INSERT INTO sources VALUES (1, 'Mandy Cane Lane', 'UC2oLKkL2ry-m7ZrtdbfFPbw', 'channel', 'https://www.youtube.com/@MandyCaneLane', 2,
  'Mandy Cane Lane - YouTube', 'all_operations', NULL, NULL, NULL, NULL, NULL);
INSERT INTO sources VALUES (2, 'Cercle', 'UCPKT_csvP72boVX0XrMtagQ', 'channel', 'https://www.youtube.com/@Cercle', 1,
  'Cercle', 'disabled', NULL, NULL, NULL, NULL, NULL);
INSERT INTO media_items VALUES (1, '7wpAfSlH5Xc', 'LEE MACK', '/downloads/TV/Mandy Cane Lane - YouTube/Mandy Cane Lane - YouTube/Season 2026/s2026e102299 - LEE MACK [7wpAfSlH5Xc].mp4',
  1, '[]', '/downloads/TV/Mandy Cane Lane - YouTube/Mandy Cane Lane - YouTube/Season 2026/s2026e102299 - LEE MACK [7wpAfSlH5Xc]-thumb.jpg', NULL, 0, 'd', 1000, 0, '2026-10-22T11:00:13Z', NULL, 755, 0, 99);
INSERT INTO media_items VALUES (2, 'y519qjVYX3U', 'Cuphead', '/downloads/Mandy Cane Lane/2017-10-07 Cuphead/Cuphead [y519qjVYX3U].mp4',
  1, '[]', NULL, NULL, 0, '', 0, 0, '2017-10-07T10:00:00Z', NULL, 600, 1, 99);
INSERT INTO media_items VALUES (3, 'cerclecerc1', 'Set', '/downloads/Cercle/2026-10-01 Set/Set [cerclecerc1].mp4',
  2, '[]', NULL, NULL, 0, '', 5000, 0, '2026-10-01T10:00:00Z', NULL, 3600, 0, 99);
INSERT INTO media_items VALUES (4, 'shortshort1', 'Short', NULL, 2, '[]', NULL, NULL, 0, '', 0, 1, '2026-10-02T10:00:00Z', NULL, 30, 0, 99);
"""


def test_migrate_pinchflat(tmp_path, monkeypatch):
    from mediafang import pinchflat, servers

    media = tmp_path / "media"
    for f in ("Pinchflat/TV/Mandy Cane Lane - YouTube/Mandy Cane Lane - YouTube/Season 2026/s2026e102299 - LEE MACK [7wpAfSlH5Xc].mp4",
              "Pinchflat/TV/Mandy Cane Lane - YouTube/Mandy Cane Lane - YouTube/Season 2026/s2026e102299 - LEE MACK [7wpAfSlH5Xc]-thumb.jpg",
              "Pinchflat/Cercle/2026-10-01 Set/Set [cerclecerc1].mp4"):
        (media / f).parent.mkdir(parents=True, exist_ok=True)
        (media / f).write_text("x")
    pf = sqlite3.connect(tmp_path / "pinchflat.db")
    pf.executescript(PINCHFLAT)
    pf.close()
    cfg = Config(
        library_dir=media / "NRK", movies_dir=media / "NRK Filmer", data_dir=tmp_path / "data",
        youtube_dir=media / "YouTube", media_root=media, pinchflat_db=tmp_path / "pinchflat.db",
        pinchflat_libraries={"Mandy Cane Lane - YouTube": "Mandy"},
    )
    db = DB(cfg.db_path)
    monkeypatch.setattr(servers, "refresh", lambda *a: None)

    assert pinchflat.migrate(cfg, db) is True  # dry run
    assert not db.sources() and (media / "Pinchflat/Cercle").exists()

    assert pinchflat.migrate(cfg, db, apply=True) is True
    mandy = db.source_by_ref("youtube", "UC2oLKkL2ry-m7ZrtdbfFPbw")
    assert (mandy.layout, mandy.library, mandy.folder, mandy.cookies) == ("tv", "Mandy", "Mandy Cane Lane - YouTube", True)
    new = media / "YouTube/Mandy/Mandy Cane Lane - YouTube/Season 2026/s2026e102299 - LEE MACK [7wpAfSlH5Xc].mp4"
    assert new.exists() and not (media / "Pinchflat/TV/Mandy Cane Lane - YouTube/Mandy Cane Lane - YouTube").exists()
    lee = db.item("youtube", "7wpAfSlH5Xc")
    assert (lee.status, lee.season, lee.episode, lee.files[0]) == ("done", 2026, 102299, str(new))
    assert db.item("youtube", "y519qjVYX3U").error == "fjernet i Pinchflat"
    assert (media / "YouTube/Video/Cercle/2026-10-01 Set/Set [cerclecerc1].mp4").exists()
    assert db.item("youtube", "shortshort1").status == "skipped"
    assert not db.due()  # nothing is downloaded again
    # running it again changes nothing
    assert pinchflat.migrate(cfg, db, apply=True) is True


def test_listing_stops_past_the_oldest_date_wanted(db, cfg, monkeypatch):
    s = channel(db, cutoff=date.today() - timedelta(days=10), livestreams=False)  # one tab
    old, new = time.time() - 60 * 86400, time.time() - 86400
    entries = [{"id": f"n{i:010d}", "timestamp": new} for i in range(3)]
    entries += [{"id": f"o{i:010d}", "timestamp": old} for i in range(1000)]
    seen = []

    class FakeYdl:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def extract_info(self, url, **k):
            def gen():
                for e in entries:
                    seen.append(e["id"])
                    yield e
            return {"entries": gen()}

    monkeypatch.setattr(youtube, "_ydl", FakeYdl)
    found = youtube.list_flat(cfg, s, set())
    assert [e.video_id for e in found] == ["n0000000000", "n0000000001", "n0000000002"]
    assert len(seen) < 10  # did not page through the old ones
