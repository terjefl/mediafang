"""The web page: search NRK TV, choose what to follow and how, and see how the downloads are going.

No login of its own; it sits behind Cloudflare Access like the other internal pages.
"""

import html
import json
import logging
import threading
import time
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import psapi
from .config import Config
from .db import DB, MAX_ATTEMPTS, EpisodeRow, Follow

log = logging.getLogger(__name__)

MODES = {
    "all": "Alt som finnes",
    "seasons": "Valgte sesonger",
    "new": "Bare nye episoder",
}
STATUS = {
    "pending": "Venter",
    "done": "Lastet ned",
    "failed": "Feilet",
    "expired": "Utløpt hos NRK",
    "geo": "Bare i Norge",
    "removed": "Ryddet bort",
}
TYPES = {"sequential": "Serie", "standard": "Datoserie", "news": "Nyheter", "": "Serie"}

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--text:#1d1d1f;--muted:#6b6b70;--line:#e3e1dc;--accent:#0b5cad;
--ok:#1e7d3a;--warn:#a35d00;--bad:#b3261e;--chip:#eeece7}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121214;--card:#1c1c1f;--text:#ececef;
--muted:#9a9aa2;--line:#2c2c31;--accent:#6aa8ff;--ok:#5cc27a;--warn:#e0a14a;--bad:#ff7a70;--chip:#26262b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:14px 16px;
border-bottom:1px solid var(--line);background:var(--card)}
header h1{font-size:18px;margin:0}header h1 a{color:var(--text)}
nav{display:flex;gap:14px;font-size:14px}
form.search{display:flex;gap:6px;flex:1 1 320px;max-width:520px}
input[type=text],input[type=search],input[type=number]{font:inherit;padding:7px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--bg);color:var(--text);min-width:0}
form.search input{flex:1}
button,.btn{font:inherit;font-size:14px;padding:7px 12px;border-radius:8px;border:1px solid var(--line);
background:var(--card);color:var(--text);cursor:pointer;white-space:nowrap}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.danger{color:var(--bad)}
main{max-width:1100px;margin:0 auto;padding:18px 16px 48px}
h2{font-size:17px;margin:22px 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.card img{width:100%;aspect-ratio:16/9;object-fit:cover;background:var(--chip);display:block}
.card .body{padding:10px 12px;display:flex;flex-direction:column;gap:6px;flex:1}
.card h3{font-size:15px;margin:0}
.muted{color:var(--muted);font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{font-size:12px;padding:2px 8px;border-radius:99px;background:var(--chip);color:var(--muted)}
.chip.ok{color:var(--ok)}.chip.warn{color:var(--warn)}.chip.bad{color:var(--bad)}
.actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:auto;padding-top:6px}
.actions form{margin:0}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600}
td.code{font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--muted)}
.tablewrap{overflow-x:auto}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:12px}
.hero{display:flex;gap:16px;flex-wrap:wrap}.hero img{width:280px;max-width:100%;border-radius:10px;aspect-ratio:16/9;object-fit:cover}
.hero .text{flex:1 1 280px}
fieldset{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:0}
legend{font-size:13px;color:var(--muted);padding:0 4px}
label.opt{display:flex;gap:8px;align-items:baseline;padding:3px 0}
.seasons{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:2px 12px;margin-top:6px}
.empty{padding:28px;text-align:center;color:var(--muted);background:var(--card);border:1px dashed var(--line);border-radius:12px}
.flash{padding:10px 12px;border-radius:10px;background:var(--chip);margin-bottom:12px}
"""


def _e(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def page(title: str, body: str, query: str = "") -> bytes:
    doc = f"""<!doctype html><html lang="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)} · NRK-arkiv</title>
<style>{CSS}</style></head><body>
<header><h1><a href="/">NRK-arkiv</a></h1>
<form class="search" action="/sok"><input type="search" name="q" value="{_e(query)}"
 placeholder="Søk i NRK TV, eller lim inn en lenke fra tv.nrk.no" aria-label="Søk"><button>Søk</button></form>
<nav><a href="/">Følger</a><a href="/logg">Logg</a></nav></header>
<main>{body}</main></body></html>"""
    return doc.encode()


def code(follow: Follow, ep: EpisodeRow) -> str:
    if follow.kind == "program":
        return "Film"
    if follow.dated:
        return f"{ep.season} #{ep.episode}"
    return f"S{ep.season:02d}E{ep.episode:02d}"


def mode_text(follow: Follow) -> str:
    if follow.kind == "program":
        return "Enkeltprogram"
    text = MODES.get(follow.mode, follow.mode)
    if follow.mode == "seasons":
        text += ": " + ", ".join(follow.seasons)
    if follow.keep_days:
        text += f" · beholder {follow.keep_days} dager"
    return text


class App:
    def __init__(self, cfg: Config, db: DB, wake: threading.Event | None = None):
        self.cfg = cfg
        self.db = db
        self.wake = wake or threading.Event()

    # ---- pages -----------------------------------------------------------------

    def index(self, flash: str = "") -> bytes:
        follows = self.db.follows()
        counts = self.db.counts()
        expiry = self.db.next_expiry()
        body = f'<div class="flash">{_e(flash)}</div>' if flash else ""
        body += """<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
<h2>Det du følger</h2><form method="post" action="/kjor"><button>Sjekk NRK nå</button></form></div>"""
        if not follows:
            body += '<div class="empty">Du følger ingenting ennå. Søk etter en serie eller film over.</div>'
            return page("Følger", body)
        cards = []
        for f in follows:
            c = counts.get(f.id, {})
            chips = [f'<span class="chip">{_e("Film" if f.kind == "program" else TYPES.get(f.series_type, "Serie"))}</span>']
            if c.get("done"):
                chips.append(f'<span class="chip ok">{c["done"]} lastet ned</span>')
            if c.get("pending"):
                chips.append(f'<span class="chip">{c["pending"]} venter</span>')
            if c.get("failed"):
                chips.append(f'<span class="chip bad">{c["failed"]} feilet</span>')
            if f.id in expiry:
                chips.append(f'<span class="chip warn">utløper {_date(expiry[f.id])}</span>')
            if not f.enabled:
                chips.append('<span class="chip warn">pauset</span>')
            toggle = "Fortsett" if not f.enabled else "Pause"
            cards.append(f"""<div class="card"><a href="/serie/{f.id}"><img src="{_e(f.image or '')}" alt="" loading="lazy"></a>
<div class="body"><h3><a href="/serie/{f.id}">{_e(f.title)}</a></h3>
<div class="muted">{_e(mode_text(f))}</div><div class="chips">{"".join(chips)}</div>
<div class="actions"><a class="btn" href="/folg?kind={f.kind}&ref={urllib.parse.quote(f.ref)}">Endre</a>
<form method="post" action="/pause"><input type="hidden" name="id" value="{f.id}"><button>{toggle}</button></form>
<form method="post" action="/slett" onsubmit="return confirm('Slutte å følge {_e(f.title)}? Filene blir liggende.')">
<input type="hidden" name="id" value="{f.id}"><button class="danger">Slutt å følge</button></form></div></div></div>""")
        body += f'<div class="grid">{"".join(cards)}</div>'
        return page("Følger", body)

    def search(self, q: str) -> bytes:
        q = q.strip()
        if not q:
            return page("Søk", '<div class="empty">Skriv noe å søke etter.</div>')
        try:
            hits = psapi.search(q)
        except Exception as e:
            return page("Søk", f'<div class="empty">Søket feilet: {_e(e)}</div>', q)
        followed = {f.ref for f in self.db.follows()}
        if not hits:
            return page("Søk", f'<div class="empty">Ingen treff på «{_e(q)}».</div>', q)
        cards = []
        for h in hits:
            kind = "Serie" if h.kind == "series" else "Film / program"
            action = (
                '<span class="chip ok">Følger</span>' if h.ref in followed else
                f'<a class="btn" href="/folg?kind={h.kind}&ref={urllib.parse.quote(h.ref)}">Følg …</a>'
            )
            cards.append(f"""<div class="card"><img src="{_e(h.image or '')}" alt="" loading="lazy"><div class="body">
<h3>{_e(h.title)}</h3><div class="chips"><span class="chip">{kind}</span></div>
<div class="muted">{_e(h.description[:220])}</div><div class="actions">{action}</div></div></div>""")
        return page("Søk", f'<h2>Treff på «{_e(q)}»</h2><div class="grid">{"".join(cards)}</div>', q)

    def follow_form(self, kind: str, ref: str) -> bytes:
        existing = self.db.follow_by_ref(ref)
        try:
            if kind == "program":
                p = psapi.program(ref)
                title, plot, image, options = p.title, p.plot, p.image, ""
                note = "" if p.available else '<div class="flash">NRK viser ikke dette programmet nå.</div>'
                if p.expires:
                    note += f'<div class="muted">Tilgjengelig hos NRK til {_date(p.expires)}.</div>'
                options = note
            else:
                s = psapi.series(ref)
                title, plot, image = s.title, s.plot, s.image
                options = self._series_options(s, existing)
        except psapi.NotFound:
            return page("Finnes ikke", '<div class="empty">NRK kjenner ikke til dette.</div>')
        verb = "Lagre" if existing else "Følg"
        body = f"""<form method="post" action="/folg" class="panel">
<input type="hidden" name="kind" value="{_e(kind)}"><input type="hidden" name="ref" value="{_e(ref)}">
<div class="hero"><img src="{_e(image or '')}" alt=""><div class="text"><h2 style="margin-top:0">{_e(title)}</h2>
<div class="muted">{_e(plot)}</div></div></div>{options}
<div><button class="primary">{verb}</button> <a class="btn" href="/">Avbryt</a></div></form>"""
        return page(title, body)

    def _series_options(self, s: psapi.Series, existing: Follow | None) -> str:
        # News goes back decades (Dagsrevyen: 600 months); take only what comes unless asked
        mode = existing.mode if existing else ("new" if s.series_type == "news" else "all")
        chosen = set(existing.seasons if existing else [])
        if s.dated:
            names = sorted({r.name[:4] for r in s.seasons if r.name[:4].isdigit()}, reverse=True)
            labels = {n: n for n in names}
            what = "år"
        else:
            names = [r.name for r in s.seasons]
            labels = {r.name: r.title for r in s.seasons}
            what = "sesonger"
        boxes = "".join(
            f'<label class="opt"><input type="checkbox" name="season" value="{_e(n)}"'
            f'{" checked" if n in chosen else ""}> {_e(labels[n])}</label>'
            for n in names
        )
        radios = "".join(
            f'<label class="opt"><input type="radio" name="mode" value="{m}"{" checked" if m == mode else ""}> {label}</label>'
            for m, label in MODES.items()
        )
        keep = ""
        if s.dated:
            days = existing.keep_days if existing and existing.keep_days else ""
            keep = f"""<fieldset><legend>Opprydding</legend><label class="opt">Behold de siste
<input type="number" name="keep_days" min="1" value="{days}" style="width:90px"> dager (tomt = behold alt)</label>
<div class="muted">Eldre sendinger slettes fra biblioteket, og bare sendinger innenfor perioden lastes ned.</div></fieldset>"""
        if s.series_type == "news":
            keep += '<div class="muted">Nyhetsserier har ofte tusenvis av sendinger; «Alt som finnes» henter alle.</div>'
        kind = TYPES.get(s.series_type, "Serie")
        count = f"{len(s.seasons)} {'perioder' if s.dated else 'sesonger'}"
        return f"""<div class="chips"><span class="chip">{kind}</span><span class="chip">{count}</span></div>
<fieldset><legend>Hva skal lastes ned?</legend>{radios}
<div class="muted">Velg {what} under hvis du velger «{MODES["seasons"]}».</div>
<div class="seasons">{boxes}</div></fieldset>{keep}"""

    def series_page(self, follow_id: int) -> bytes | None:
        follow = self.db.follow(follow_id)
        if not follow:
            return None
        eps = self.db.episodes(follow.id)
        rows = []
        for ep in eps:
            status = STATUS.get(ep.status, ep.status)
            if ep.status == "pending" and ep.retry_after > time.time():
                status = "Ikke lagt ut ennå"
            if ep.status == "failed" and ep.attempts >= MAX_ATTEMPTS:
                status += " (gitt opp)"
            retry = ""
            if ep.status in ("failed", "expired", "geo", "removed"):
                retry = f"""<form method="post" action="/prov-igjen" style="margin:0"><input type="hidden" name="prf" value="{_e(ep.prf_id)}">
<input type="hidden" name="back" value="{follow.id}"><button>Prøv igjen</button></form>"""
            err = f'<div class="muted">{_e(ep.error[-200:])}</div>' if ep.error and ep.status != "done" else ""
            rows.append(f"""<tr><td class="code">{_e(code(follow, ep))}</td><td>{_e(ep.title)}{err}</td>
<td class="code">{_date(ep.aired)}</td><td class="code">{_date(ep.expires)}</td><td>{_e(status)}</td><td>{retry}</td></tr>""")
        table = (
            '<div class="tablewrap"><table><tr><th>Nr</th><th>Tittel</th><th>Sendt</th><th>Utløper</th><th>Status</th><th></th></tr>'
            + "".join(rows) + "</table></div>"
            if rows else '<div class="empty">Ingen episoder funnet ennå. De dukker opp etter neste sjekk.</div>'
        )
        body = f"""<div class="panel"><div class="hero"><img src="{_e(follow.image or '')}" alt="">
<div class="text"><h2 style="margin-top:0">{_e(follow.title)}</h2><div class="muted">{_e(follow.plot)}</div>
<p class="muted">{_e(mode_text(follow))}</p>
<a class="btn" href="/folg?kind={follow.kind}&ref={urllib.parse.quote(follow.ref)}">Endre</a>
<a class="btn" href="https://tv.nrk.no/{'serie' if follow.kind == 'series' else 'program'}/{_e(follow.ref)}">Se hos NRK</a>
</div></div></div><h2>Episoder</h2>{table}"""
        return page(follow.title, body)

    def log_page(self) -> bytes:
        rows = []
        for ep, show in self.db.recent():
            when = datetime.fromtimestamp(ep.updated).strftime("%d.%m %H:%M")
            err = f'<div class="muted">{_e(ep.error[-200:])}</div>' if ep.error and ep.status != "done" else ""
            rows.append(f"""<tr><td class="code">{when}</td><td><a href="/serie/{ep.follow_id}">{_e(show)}</a></td>
<td>{_e(ep.title)}{err}</td><td>{_e(STATUS.get(ep.status, ep.status))}</td></tr>""")
        if not rows:
            return page("Logg", '<div class="empty">Ingenting har skjedd ennå.</div>')
        return page("Logg", '<h2>Siste hendelser</h2><div class="tablewrap"><table><tr><th>Når</th><th>Serie</th>'
                    '<th>Episode</th><th>Status</th></tr>' + "".join(rows) + "</table></div>")

    # ---- actions ---------------------------------------------------------------

    def save_follow(self, form: dict[str, list[str]]) -> str:
        kind = form.get("kind", ["series"])[0]
        ref = form.get("ref", [""])[0]
        if kind not in ("series", "program") or not ref:
            return "/"
        if kind == "program":
            p = psapi.program(ref)
            self.db.save_follow("program", ref, p.title, image=p.image, plot=p.plot, poster=p.poster, backdrop=p.backdrop)
        else:
            s = psapi.series(ref)
            mode = form.get("mode", ["all"])[0]
            seasons = form.get("season", [])
            if mode == "seasons" and not seasons:
                mode = "all"
            keep = form.get("keep_days", [""])[0].strip()
            self.db.save_follow(
                "series", s.slug, s.title, mode=mode if mode in MODES else "all", seasons=seasons,
                keep_days=int(keep) if keep.isdigit() and int(keep) > 0 else None,
                image=s.image, plot=s.plot, series_type=s.series_type, poster=s.poster, backdrop=s.backdrop,
            )
        self.wake.set()
        return "/?ok=1"


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("web: " + fmt, *args)

        def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            try:
                if url.path == "/":
                    flash = "Lagret. NRK sjekkes nå, nye episoder lastes ned i bakgrunnen." if q.get("ok") else ""
                    return self._send(200, app.index(flash))
                if url.path == "/health":
                    return self._send(200, json.dumps({"ok": True}).encode(), "application/json")
                if url.path == "/sok":
                    text = q.get("q", [""])[0]
                    if parsed := psapi.parse_url(text):
                        return self._redirect(f"/folg?kind={parsed[0]}&ref={urllib.parse.quote(parsed[1])}")
                    return self._send(200, app.search(text))
                if url.path == "/folg":
                    return self._send(200, app.follow_form(q.get("kind", ["series"])[0], q.get("ref", [""])[0]))
                if url.path.startswith("/serie/") and url.path[7:].isdigit():
                    body = app.series_page(int(url.path[7:]))
                    if body:
                        return self._send(200, body)
                if url.path == "/logg":
                    return self._send(200, app.log_page())
                self._send(404, page("Ikke funnet", '<div class="empty">Siden finnes ikke.</div>'))
            except Exception as e:
                log.exception("web: GET %s failed", self.path)
                self._send(500, page("Feil", f'<div class="empty">Noe gikk galt: {_e(e)}</div>'))

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            path = urllib.parse.urlparse(self.path).path
            try:
                if path == "/folg":
                    return self._redirect(app.save_follow(form))
                if path == "/kjor":
                    app.wake.set()
                    return self._redirect("/")
                if path in ("/pause", "/slett") and form.get("id", [""])[0].isdigit():
                    follow = app.db.follow(int(form["id"][0]))
                    if follow and path == "/pause":
                        app.db.set_enabled(follow.id, not follow.enabled)
                    elif follow:
                        app.db.delete_follow(follow.id)
                    return self._redirect("/")
                if path == "/prov-igjen" and form.get("prf"):
                    app.db.retry(form["prf"][0])
                    app.wake.set()
                    back = form.get("back", [""])[0]
                    return self._redirect(f"/serie/{back}" if back.isdigit() else "/")
                self._send(404, page("Ikke funnet", '<div class="empty">Siden finnes ikke.</div>'))
            except Exception as e:
                log.exception("web: POST %s failed", self.path)
                self._send(500, page("Feil", f'<div class="empty">Noe gikk galt: {_e(e)}</div>'))

    return Handler


def serve(app: App, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    log.info("Web page on port %d", port)
    return server
