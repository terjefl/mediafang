import argparse
import logging
import threading
from pathlib import Path

from . import psapi
from .config import Config, load_config
from .db import DB
from .harvest import Harvester
from .servers import heartbeat, refresh_jellyfin, refresh_plex

log = logging.getLogger("nrktv")


def run_once(cfg: Config, db: DB, stop=None) -> bool:
    result = Harvester(cfg, db).run(stop=stop)
    if result.tv_changed or result.movies_changed:
        refresh_plex(cfg, tv=result.tv_changed, movies=result.movies_changed)
        refresh_jellyfin(cfg)
    msg = (
        f"{result.found} new, {len(result.downloaded)} downloaded, {len(result.failed)} failed, "
        f"{result.removed} removed"
    )
    if result.errors:
        msg += " | " + "; ".join(result.errors)
    log.info(msg)
    ok = not result.failed and not result.errors
    heartbeat(cfg.heartbeat_url, ok, msg)
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(prog="nrktv", description="NRK TV series and films in Plex and Jellyfin")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    p_search = sub.add_parser("search", help="search NRK TV")
    p_search.add_argument("query")
    p_add = sub.add_parser("add", help="follow a series or program (a tv.nrk.no link or program id)")
    p_add.add_argument("url")
    p_add.add_argument("--mode", choices=("all", "seasons", "new"), default="all")
    p_add.add_argument("--seasons", default="", help="comma-separated seasons (date series: years) for --mode seasons")
    p_add.add_argument("--keep-days", type=int, help="date series: keep only this many days")
    sub.add_parser("list", help="show what is followed")
    sub.add_parser("scan", help="look for new episodes without downloading")
    sub.add_parser("refresh-art", help="rewrite show NFOs, posters and backgrounds")
    sub.add_parser("run", help="look for and download new episodes once")
    sub.add_parser("loop", help="the web page, plus a run every interval_minutes")
    sub.add_parser("web", help="only the web page (no downloads)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    db = DB(cfg.db_path)

    if args.command == "search":
        for hit in psapi.search(args.query):
            print(f"{hit.kind:8} {hit.ref:28} {hit.title}")
    elif args.command == "add":
        parsed = psapi.parse_url(args.url)
        if not parsed:
            raise SystemExit(f"Not a tv.nrk.no series or program link: {args.url}")
        kind, ref = parsed
        if kind == "program":
            p = psapi.program(ref)
            follow = db.save_follow(kind, ref, p.title, image=p.image, plot=p.plot, poster=p.poster, backdrop=p.backdrop)
        else:
            s = psapi.series(ref)
            seasons = [x.strip() for x in args.seasons.split(",") if x.strip()]
            follow = db.save_follow(
                kind, s.slug, s.title, mode=args.mode, seasons=seasons, keep_days=args.keep_days,
                image=s.image, plot=s.plot, series_type=s.series_type, poster=s.poster, backdrop=s.backdrop,
            )
        print(f"Following {follow.title} ({follow.kind}, {follow.mode})")
    elif args.command == "list":
        counts = db.counts()
        for f in db.follows():
            c = counts.get(f.id, {})
            print(f"{f.id:4} {f.title:40} {f.kind:8} {f.mode:8} {dict(c)}{'' if f.enabled else ' (paused)'}")
    elif args.command == "scan":
        h = Harvester(cfg, db)
        for f in db.follows(enabled_only=True):
            print(f"{f.title}: {h.scan(f)} new")
    elif args.command == "refresh-art":
        print(f"{Harvester(cfg, db).refresh_art()} show(s) refreshed")
    elif args.command == "run":
        raise SystemExit(0 if run_once(cfg, db) else 1)
    elif args.command == "web":
        from .web import App, make_handler

        from http.server import ThreadingHTTPServer

        log.info("Web page on port %d", cfg.web_port)
        ThreadingHTTPServer(("0.0.0.0", cfg.web_port), make_handler(App(cfg, db))).serve_forever()
    elif args.command == "loop":
        wake = threading.Event()
        if cfg.web_port:
            from .web import App, serve

            serve(App(cfg, db, wake), cfg.web_port)
        while True:
            wake.clear()
            try:
                run_once(cfg, db)
            except Exception:  # one bad run must not stop the loop
                log.exception("Run failed")
            log.info("Sleeping %d minutes", cfg.interval_minutes)
            # The web page wakes us when something new is followed
            wake.wait(cfg.interval_minutes * 60)


if __name__ == "__main__":
    main()
