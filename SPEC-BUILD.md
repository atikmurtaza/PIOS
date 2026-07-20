# PIOS v0 — Build Spec (single-user test build)

This is the binding contract for the v0 prototype. It implements the five MVP
capabilities of the MVP at minimum viable depth, **fully local**
(zero cloud egress — cloud routing is deferred; the egress log exists to prove
nothing leaves).

## Runtime & layout

- Python 3.14, venv at `.venv`. Third-party deps ONLY: `fastapi`, `uvicorn`,
  `watchdog` (in `requirements.txt`). Everything else stdlib.
- Local LLM: Ollama at `http://127.0.0.1:11434`, model **`gemma4`** (installed).
  LLM is an enhancer, never a dependency: every feature must work (degraded but
  truthful) when Ollama is down.
- Data dir: `%LOCALAPPDATA%\PIOS\` → `pios.db`, `config.json`. Tests use tmp dirs.
- Server: `http://127.0.0.1:8321`, loopback only.

```
pios/
  __init__.py        (version string)
  db.py        E1    storage + FTS5 + all queries
  llm.py       E1    Ollama adapter
  memory.py    E1    episodizer, retrieval, brief, chat answerer
  sensors.py   E2    window sensor, idle, folder watcher
  config.py    E2    config load/save
  api.py       E3    FastAPI routes
  main.py      E3    entrypoint: threads + uvicorn
ui/index.html  E3    the whole UI, one file
tests/         E3    pytest
requirements.txt E3
run_pios.bat   E3    create venv if missing, install, launch, open browser
```

## Module contracts

### db.py (E1)
```python
DB_PATH  # default %LOCALAPPDATA%\PIOS\pios.db, overridable via PIOS_DB env var
def connect(path=None) -> sqlite3.Connection   # WAL, row_factory=Row, creates schema
# events: id, ts (unix float), source ('window'|'file'|'chat'), app, title, detail, dur_s
def insert_event(con, ev: dict) -> int
def recent_events(con, since_ts) -> list[Row]
# episodes: id, start_ts, end_ts, summary, apps (csv), source_event_ids (csv)  + FTS5 on summary
def insert_episode(con, ep: dict) -> int
def episodes_between(con, t0, t1) -> list[Row]
def delete_episode(con, ep_id)                  # cascades: also deletes its FTS row
def search(con, query, limit=8) -> list[Row]    # FTS5 over episodes, recency-boosted;
                                                # MUST NOT raise on user punctuation (sanitize query)
# egress: id, ts, destination, payload, allowed  — v0 writes nothing to it; API reads it
def egress_rows(con) -> list[Row]
def stats(con) -> dict  # counts: events, episodes, first_ts, last_ts
```

### llm.py (E1)
```python
def available() -> bool                      # GET /api/tags, short timeout, never raises
def complete(prompt, system=None, timeout=60) -> str | None   # POST /api/generate, model gemma4
```
stdlib urllib only. `None` on any failure — callers must handle it.

### memory.py (E1)
```python
def episodize(con, now=None) -> int
# Groups un-episodized window/file events older than 15 min into blocks split on
# >10-min gaps. Heuristic summary ALWAYS (top apps + representative titles + duration
# — built only from real event data, no invention). If llm.available(): rewrite as one
# fluent sentence, keeping the heuristic as fallback. Store with source_event_ids.
def retrieve(con, query) -> list[dict]       # db.search + today's recent events
def build_brief(con) -> str                  # today (+yesterday): time by app/project,
                                             # notable episodes, last thing worked on.
                                             # Heuristic skeleton + optional LLM polish.
def answer(con, question) -> dict            # {'answer': str, 'sources': [episode ids],
                                             # 'model': 'gemma4'|'retrieval-only'}
# retrieval-grounded: context = retrieve(); LLM answers ONLY from context with sources;
# if LLM down, return formatted matching episodes. Log the exchange as a 'chat' event.
```

### sensors.py (E2)
```python
class WindowSensor:   # thread; poll every 3s via ctypes Win32
    def __init__(self, emit: Callable[[dict], None], poll_s=3.0): ...
    def start(self); def stop(self)
# Emits ONLY on focus change (app or title changed), with dur_s of the previous window:
# {'source':'window','app':'Code.exe','title':'main.py — VS Code','ts':...,'dur_s':...}
# Idle: GetLastInputInfo; if idle >120s, close current window span, emit nothing until
# activity resumes. Exclude own UI (title contains 'PIOS'). Never crash the thread.
class FolderSensor:   # watchdog observer over config['watched_folders']
# Emits {'source':'file','app':'fs','title':'modified README.md','detail': full_path, ...}
# Debounce: same path within 30s emits once. Ignore dirs: .git, node_modules, .venv, __pycache__.
def start_all(config, emit) -> list  # returns started sensors; skips folder sensor if no folders
```

### config.py (E2)
```python
DEFAULTS = {'watched_folders': [], 'window_sensor': True, 'file_sensor': True,
            'poll_seconds': 3.0, 'idle_seconds': 120}
def load() -> dict            # %LOCALAPPDATA%\PIOS\config.json, merged over DEFAULTS
def save(cfg: dict)
```

### api.py + main.py (E3)
Routes (JSON):
```
GET  /                → ui/index.html
GET  /api/status      → {running, ollama, db stats, sensors, version}
POST /api/chat        {message} → memory.answer()
GET  /api/brief       → {brief}
GET  /api/timeline    → today's episodes + last 50 raw events
GET  /api/memory?q=   → episodes (all recent, or FTS search results)
DELETE /api/memory/{id}
GET  /api/egress      → rows (v0: empty = proof of zero egress) + explanation string
GET  /api/config, POST /api/config   (toggles + watched folders)
POST /api/episodize   → force an episodize pass now (for testing)
```
main.py: parse `--port`, connect db, start sensors (emit → insert_event with its own
connection per thread — sqlite connections are not shared across threads), start
episodizer daemon thread (every 5 min: episodize()), uvicorn on 127.0.0.1.
Graceful Ctrl+C. `python -m pios.main` must work.

### ui/index.html (E3)
One file, no external resources. Tabs: **Chat** (streaming not required; show
sources), **Today** (brief + timeline), **Memory** (search, list, delete buttons),
**Privacy** (sensor toggles, watched-folder editor, egress log with the zero-egress
explanation, data-dir path, "delete all data" NOT in v0 — deletion is per-memory).
Header shows status dot (core/Ollama). Clean, dark-friendly, no framework.

### tests/ (E3)
pytest, temp DB via PIOS_DB env var, no Ollama dependency (monkeypatch
`llm.available` → False). Cover: schema + insert/search roundtrip; episodize on a
synthetic day of events (assert truthful summary, provenance ids, gap splitting);
FTS query sanitization (punctuation doesn't 500); API smoke on every route via
TestClient; DELETE /api/memory removes from FTS too.

## Privacy invariants (CI-grade, enforced in code review)
1. Only `llm.py` may open a network connection, and only to 127.0.0.1:11434.
2. Server binds 127.0.0.1 only.
3. No screenshots, no keylogging, no window contents — titles/paths/process names only.
4. Every episode carries `source_event_ids` (provenance) — episodes without it are bugs.
5. UI loads zero external resources.
