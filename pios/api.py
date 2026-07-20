"""PIOS local API. E3-owned. FastAPI, loopback only (main.py binds 127.0.0.1).

No global db connection: sqlite3 connections are thread-bound and uvicorn
serves requests from a threadpool, so every request opens its own connection
(cheap with WAL) via the `con` dependency.
"""
import os
import secrets
import time
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import __version__, config, db, gate, llm, memory

app = FastAPI(title="PIOS", version=__version__)

UI_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "ui", "index.html")

EGRESS_EXPLANATION = (
    "Every byte PIOS sends to a cloud model — or prepares for you to paste "
    "into a web AI — is logged here, verbatim, after being scrubbed of "
    "emails, tokens, paths and IPs. Cloud routing is off by default; while "
    "it's off this log stays empty and nothing leaves your machine except "
    "local calls to Ollama at 127.0.0.1. Routing order when on: Gemini free "
    "tier first, then paid APIs only if you explicitly allow them.")


def _con():
    con = db.connect()
    try:
        yield con
    finally:
        con.close()


def _midnight():
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


class ChatIn(BaseModel):
    message: str
    cloud: bool = False


class AssistImportIn(BaseModel):
    question: str
    response: str
    provider: str = "web"


class GitImportIn(BaseModel):
    repos: list[str] = []
    since_days: int = 90


MAX_VISIT_S = 8 * 3600      # a laptop sleeping mid-tab must not log a 20h visit
MAX_BROWSER_BATCH = 50
MAX_BROWSER_BODY = 256 * 1024


class BrowserVisit(BaseModel):
    url: str = Field(max_length=2048)
    title: str = Field(default="", max_length=512)
    dur_s: float = 0.0


class BrowserIn(BaseModel):
    token: str = ""
    events: list[BrowserVisit] = []


def browser_token() -> str:
    """The shared secret the extension must present. Generated once on first
    use and persisted, because /api/events/browser is reachable from any page
    the user visits and must not accept anonymous writes."""
    cfg = config.load()
    tok = cfg.get("browser_token") or ""
    if not tok:
        tok = secrets.token_urlsafe(24)
        cfg["browser_token"] = tok
        config.save(cfg)
    return tok


def _blocked(host: str, blocklist) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d)
               for d in (str(x).lower().strip() for x in blocklist) if d)


@app.get("/")
def index():
    return FileResponse(UI_PATH, media_type="text/html")


@app.get("/api/status")
def status(con=Depends(_con)):
    cfg = config.load()
    return {
        "running": True,
        "version": __version__,
        "ollama": llm.available(),
        "db": db.stats(con),
        "sensors": {"window_sensor": cfg.get("window_sensor", True),
                    "file_sensor": cfg.get("file_sensor", True),
                    "watched_folders": cfg.get("watched_folders", [])},
        "data_dir": os.path.dirname(os.path.abspath(
            os.environ.get("PIOS_DB") or db.DB_PATH)),
    }


@app.post("/api/chat")
def chat(body: ChatIn, con=Depends(_con)):
    return memory.answer(con, body.message, cloud=body.cloud)


@app.get("/api/brief")
def brief(con=Depends(_con)):
    return {"brief": memory.build_brief(con)}


@app.get("/api/timeline")
def timeline(con=Depends(_con)):
    now = time.time()
    eps = db.episodes_between(con, _midnight(), now)
    events = db.recent_events(con, _midnight() - 86400)[-50:]
    return {"episodes": [dict(e) for e in eps],
            "events": [dict(e) for e in events]}


@app.get("/api/memory")
def memory_list(q: str = "", con=Depends(_con)):
    if q.strip():
        rows = db.search(con, q, limit=50)
    else:
        rows = db.episodes_between(con, 0, time.time())[-100:][::-1]
    return {"episodes": [dict(r) for r in rows]}


@app.delete("/api/memory/{ep_id}")
def memory_delete(ep_id: int, con=Depends(_con)):
    if not con.execute("SELECT 1 FROM episodes WHERE id=?", (ep_id,)).fetchone():
        raise HTTPException(404, "no such episode")
    db.delete_episode(con, ep_id)
    return {"deleted": ep_id}


@app.get("/api/facts")
def facts_list(con=Depends(_con)):
    return {"facts": [dict(r) for r in db.facts_all(con)]}


@app.delete("/api/facts/{fid}")
def facts_delete(fid: int, con=Depends(_con)):
    if not con.execute("SELECT 1 FROM facts WHERE id=?", (fid,)).fetchone():
        raise HTTPException(404, "no such fact")
    db.delete_fact(con, fid)
    return {"deleted": fid}


@app.post("/api/assist/import")
def assist_import(body: AssistImportIn, con=Depends(_con)):
    if not body.response.strip():
        raise HTTPException(400, "empty response")
    ep = memory.import_assist(con, body.question, body.response, body.provider)
    return {"stored": True, "episode": ep}


@app.post("/api/git/import")
def git_import(body: GitImportIn, con=Depends(_con)):
    """Back-fill memory from repo history (the cold-start fix).

    gitimport re-emits on every call by design (it doesn't own the DB), so
    dedupe here on the commit sha, which it puts first in `detail`.
    """
    from . import gitimport
    repos = body.repos or config.load().get("git_repos", [])
    seen = {(r["detail"] or "").split(" ")[0]
            for r in con.execute(
                "SELECT detail FROM events WHERE source='git'").fetchall()}
    added = 0

    def emit(ev):
        nonlocal added
        sha = (ev.get("detail") or "").split(" ")[0]
        if sha and sha in seen:
            return
        seen.add(sha)
        db.insert_event(con, ev)
        added += 1

    for repo in repos:
        try:
            gitimport.import_repo(repo, emit, since_days=body.since_days)
        except Exception as e:
            raise HTTPException(400, "%s: %s" % (repo, e))
    return {"imported": added, "repos": len(repos)}


@app.post("/api/events/browser")
async def browser_events(body: BrowserIn, request: Request, con=Depends(_con)):
    """Ingest completed browser visits from the extension.

    Untrusted boundary: any web page the user visits can reach this port, so
    (a) a shared token is required, (b) the JSON content type forces a CORS
    preflight that a cross-origin page cannot satisfy (no CORS middleware is
    installed — the extension uses host_permissions and needs none), and
    (c) the domain blocklist is re-applied here, never trusting the client.
    """
    try:
        if int(request.headers.get("content-length") or 0) > MAX_BROWSER_BODY:
            raise HTTPException(413, "payload too large")
    except ValueError:
        raise HTTPException(400, "bad content-length")
    if not secrets.compare_digest(body.token or "", browser_token()):
        raise HTTPException(401, "bad or missing browser token")
    if len(body.events) > MAX_BROWSER_BATCH:
        raise HTTPException(413, "batch too large")

    cfg = config.load()
    if not cfg.get("browser_sensor", True):
        return {"stored": 0, "skipped": len(body.events), "sensor": "off"}

    blocklist = cfg.get("blocked_domains", [])
    stored = 0
    for v in body.events:
        parts = urlsplit(v.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        if _blocked(parts.hostname, blocklist):
            continue
        host = parts.hostname.lower()
        db.insert_event(con, {
            "source": "browser",
            "app": host[4:] if host.startswith("www.") else host,
            "title": v.title.strip() or v.url,
            "detail": v.url,
            "ts": time.time(),
            "dur_s": max(0.0, min(float(v.dur_s), MAX_VISIT_S)),
        })
        stored += 1
    return {"stored": stored, "skipped": len(body.events) - stored}


@app.get("/api/extension/config")
def extension_config():
    """Readable without the token on purpose: it leaks nothing (the blocklist
    is a list of well-known domains, not user data) and the extension needs it
    before the user has pasted a token in."""
    cfg = config.load()
    return {"blocked_domains": cfg.get("blocked_domains", []),
            "browser_sensor": cfg.get("browser_sensor", True)}


@app.post("/api/consolidate")
def consolidate(con=Depends(_con)):
    return {"created": memory.consolidate(con)}


@app.get("/api/resume")
def resume(con=Depends(_con)):
    return memory.resume(con)


@app.get("/api/egress")
def egress(con=Depends(_con)):
    cfg = config.load()
    ok, reason = gate.cloud_allowed(cfg)
    return {"rows": [dict(r) for r in db.egress_rows(con)],
            "explanation": EGRESS_EXPLANATION,
            "cloud_enabled": cfg.get("cloud_enabled", False),
            "cloud_status": reason}


@app.get("/api/config")
def config_get():
    browser_token()  # generate on first read so the UI can always show one
    return {"config": config.load(),
            "note": "Sensor changes take effect after restarting PIOS."}


@app.post("/api/config")
def config_post(body: dict):
    cfg = config.load()
    cfg.update({k: v for k, v in body.items() if k in config.DEFAULTS})
    config.save(cfg)
    return {"config": cfg,
            "note": "Saved. Sensor changes take effect after restarting PIOS."}


@app.post("/api/episodize")
def episodize(con=Depends(_con)):
    return {"created": memory.episodize(con)}
