import argparse
import logging
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import providers
from .config import Config, load_config
from .db import DB
from .harvest import Harvester, Progress
from .servers import heartbeat, refresh

log = logging.getLogger("mediafang")

# Starting points for a new source, also offered on the web page
PRESETS = {
    "tv": {"layout": "tv"},
    "videos": {"layout": "videos"},
    "audio": {"layout": "audio", "quality": 0},
}


def run_once(cfg: Config, db: DB, progress: Progress | None = None) -> bool:
    result = Harvester(cfg, db, progress).run()
    if result.changed:
        refresh(cfg, sorted(result.changed))
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


def follow(db: DB, text: str, **settings):
    """Follow what a link points at, with the provider's own title and pictures."""
    parsed = providers.parse_url(text)
    if not parsed:
        raise SystemExit(f"Not an NRK TV or YouTube link: {text}")
    provider, kind, ref = parsed
    info = providers.get(provider).lookup(kind, ref)
    if provider == "nrk":
        settings.setdefault("layout", "movie" if kind == "program" else "tv")
    else:
        settings.setdefault("layout", "tv")
        settings.setdefault("url", info.url)
    return db.save_source(
        provider, info.kind, info.ref, info.title, plot=info.plot, image=info.image, poster=info.poster,
        backdrop=info.backdrop, series_type=info.series_type, **settings,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="mediafang", description="NRK TV and YouTube in Plex and Jellyfin")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    p_search = sub.add_parser("search", help="search NRK TV (and YouTube with --youtube)")
    p_search.add_argument("query")
    p_search.add_argument("--youtube", action="store_true")
    p_add = sub.add_parser("add", help="follow an NRK series/program or a YouTube channel/playlist (a link)")
    p_add.add_argument("url")
    p_add.add_argument("--mode", choices=("all", "seasons", "new"), default="all")
    p_add.add_argument("--seasons", default="", help="comma-separated seasons (date series: years) for --mode seasons")
    p_add.add_argument("--keep-days", type=int, help="keep only this many days")
    p_add.add_argument("--preset", choices=sorted(PRESETS), help="YouTube: tv, videos or audio")
    p_add.add_argument("--library", default="", help="YouTube: library folder, e.g. TV or Video")
    p_add.add_argument("--folder", default="", help="YouTube: the channel's folder name (default its title)")
    sub.add_parser("list", help="show what is followed")
    sub.add_parser("scan", help="look for new items without downloading")
    sub.add_parser("refresh-art", help="rewrite show NFOs, posters and backgrounds")
    sub.add_parser("run", help="look for and download new items once")
    sub.add_parser("loop", help="the web page, plus a run every interval_minutes")
    sub.add_parser("web", help="only the web page (no downloads)")
    p_mig = sub.add_parser("migrate-pinchflat", help="take over Pinchflat's sources and files")
    p_mig.add_argument("--apply", action="store_true", help="import and move (default: only show the plan)")
    p_mig.add_argument("--finish", action="store_true", help="remove the old folders from the libraries")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    db = DB(cfg.db_path)

    if args.command == "search":
        hits = providers.get("youtube").search(args.query) if args.youtube else providers.get("nrk").search(args.query)
        for hit in hits:
            print(f"{hit.provider:8} {hit.kind:8} {hit.ref:28} {hit.title}")
    elif args.command == "add":
        settings = dict(PRESETS.get(args.preset, {}))
        settings.update(
            mode=args.mode, seasons=[x.strip() for x in args.seasons.split(",") if x.strip()],
            keep_days=args.keep_days,
        )
        if args.library:
            settings["library"] = args.library
        if args.folder:
            settings["folder"] = args.folder
        source = follow(db, args.url, **settings)
        print(f"Following {source.title} ({source.provider} {source.kind}, {source.layout}, {source.mode})")
    elif args.command == "list":
        counts = db.counts()
        for s in db.sources():
            c = counts.get(s.id, {})
            print(f"{s.id:4} {s.provider:8} {s.title:40} {s.layout:7} {s.mode:8} {dict(c)}{'' if s.enabled else ' (paused)'}")
    elif args.command == "scan":
        h = Harvester(cfg, db)
        for s in db.sources(enabled_only=True):
            print(f"{s.title}: {h.scan(s)} new")
    elif args.command == "refresh-art":
        print(f"{Harvester(cfg, db).refresh_art()} show(s) refreshed")
    elif args.command == "run":
        raise SystemExit(0 if run_once(cfg, db) else 1)
    elif args.command == "web":
        from .web import App, make_handler

        log.info("Web page on port %d", cfg.web_port)
        ThreadingHTTPServer(("0.0.0.0", cfg.web_port), make_handler(App(cfg, db))).serve_forever()
    elif args.command == "migrate-pinchflat":
        from .pinchflat import migrate

        raise SystemExit(0 if migrate(cfg, db, apply=args.apply, finish=args.finish) else 1)
    elif args.command == "loop":
        wake = threading.Event()
        progress = Progress()
        if cfg.web_port:
            from .web import App, serve

            serve(App(cfg, db, wake, progress), cfg.web_port)
        while True:
            wake.clear()
            try:
                run_once(cfg, db, progress)
            except Exception:  # one bad run must not stop the loop
                log.exception("Run failed")
            log.info("Sleeping %d minutes", cfg.interval_minutes)
            # The web page wakes us when something new is followed
            wake.wait(cfg.interval_minutes * 60)


if __name__ == "__main__":
    main()
