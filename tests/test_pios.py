"""PIOS test suite. Temp DB/config via env vars; never touches real data or Ollama."""
import os
import tempfile
import time

import pytest

# Point PIOS at a throwaway dir BEFORE importing pios modules (db.py computes
# DB_PATH at import time). Per-test isolation comes from the fixtures below —
# db.connect() and config._path() re-read the env vars on every call.
_tmp = tempfile.mkdtemp(prefix="pios-test-")
os.environ["PIOS_DB"] = os.path.join(_tmp, "boot.db")
os.environ["PIOS_CONFIG"] = os.path.join(_tmp, "boot-config.json")

from fastapi.testclient import TestClient  # noqa: E402

from pios import api, config, db, llm, memory  # noqa: E402


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """No test may reach Ollama."""
    monkeypatch.setattr(llm, "available", lambda: False)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PIOS_DB", str(tmp_path / "pios.db"))
    monkeypatch.setenv("PIOS_CONFIG", str(tmp_path / "config.json"))


@pytest.fixture
def con():
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        yield c


def _ep(con, summary="worked on pios api in Code.exe", start=None, end=None):
    start = start or time.time() - 3600
    return db.insert_episode(con, {
        "start_ts": start, "end_ts": end or start + 600,
        "summary": summary, "apps": "Code.exe",
        "source_event_ids": "1,2"})


# ---------- db ----------

def test_db_roundtrip_and_fts_search(con):
    eid = db.insert_event(con, {"source": "window", "app": "Code.exe",
                                "title": "main.py", "dur_s": 120})
    assert eid == 1
    rows = db.recent_events(con, 0)
    assert len(rows) == 1 and rows[0]["app"] == "Code.exe"

    ep_id = _ep(con)
    hits = db.search(con, "pios api")
    assert [h["id"] for h in hits] == [ep_id]
    assert db.stats(con) == {"events": 1, "episodes": 1,
                             "first_ts": rows[0]["ts"], "last_ts": rows[0]["ts"]}


def test_fts_punctuation_never_raises(con):
    _ep(con, "debugging C++ what's-it code")
    for q in ["what's up?", "C++!!", '"quoted" (parens) *star*', "-", "'''", ""]:
        db.search(con, q)  # must not raise
    assert db.search(con, "?!.,") == []  # punctuation-only -> empty, no 500


def test_delete_episode_removes_fts(con):
    ep_id = _ep(con, "unique zanzibar topic")
    assert db.search(con, "zanzibar")
    db.delete_episode(con, ep_id)
    assert db.search(con, "zanzibar") == []
    assert con.execute("SELECT * FROM episodes_fts WHERE rowid=?",
                       (ep_id,)).fetchall() == []


# ---------- memory ----------

def test_episodize_gap_split_provenance_and_freshness(con):
    now = time.time()
    base = now - 3 * 3600  # 3h ago: old enough to episodize
    ids = []
    # block 1: two contiguous window events
    ids.append(db.insert_event(con, {"source": "window", "app": "Code.exe",
                                     "title": "main.py", "ts": base, "dur_s": 300}))
    ids.append(db.insert_event(con, {"source": "window", "app": "Code.exe",
                                     "title": "db.py", "ts": base + 300, "dur_s": 300}))
    # >10-min gap, then block 2
    ids.append(db.insert_event(con, {"source": "window", "app": "firefox.exe",
                                     "title": "docs", "ts": base + 300 + 300 + 1200,
                                     "dur_s": 600}))
    # fresh event (< 15 min old): must be left alone
    fresh = db.insert_event(con, {"source": "window", "app": "Code.exe",
                                  "title": "fresh.py", "ts": now - 60, "dur_s": 30})

    assert memory.episodize(con, now=now) == 2
    eps = db.episodes_between(con, 0, now)
    assert len(eps) == 2
    assert [e["source_event_ids"] for e in eps] == ["%d,%d" % (ids[0], ids[1]),
                                                    str(ids[2])]
    # truthful summaries from real data
    assert "Code.exe" in eps[0]["summary"] and "firefox.exe" in eps[1]["summary"]
    # old events flagged, fresh one untouched
    flags = dict(con.execute("SELECT id, episodized FROM events").fetchall())
    assert flags[ids[0]] == flags[ids[1]] == flags[ids[2]] == 1
    assert flags[fresh] == 0
    # second pass is a no-op
    assert memory.episodize(con, now=now) == 0


def test_answer_retrieval_only_shape(con):
    ep_id = _ep(con, "wrote the frobnicator module")
    r = memory.answer(con, "what about the frobnicator?")
    assert set(r) == {"answer", "sources", "model"}
    assert r["model"] == "retrieval-only"
    assert r["sources"] == [ep_id]
    assert "frobnicator" in r["answer"]
    # exchange logged as a chat event
    chats = [e for e in db.recent_events(con, 0) if e["source"] == "chat"]
    assert len(chats) == 1

    r2 = memory.answer(con, "zzz-no-such-thing-qqq")
    assert r2["model"] == "retrieval-only" and r2["sources"] == []


# ---------- api ----------

def test_api_root_serves_ui(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "PIOS" in r.text and "<html" in r.text.lower()


def test_api_status(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    j = r.json()
    assert j["running"] is True and j["ollama"] is False
    assert set(j["db"]) == {"events", "episodes", "first_ts", "last_ts"}
    assert "window_sensor" in j["sensors"]


def test_api_chat(client):
    r = client.post("/api/chat", json={"message": "what did I do today?"})
    assert r.status_code == 200
    assert set(r.json()) == {"answer", "sources", "model"}


def test_api_brief_timeline_episodize(client):
    con = db.connect()
    db.insert_event(con, {"source": "window", "app": "Code.exe",
                          "title": "main.py", "ts": time.time() - 3600,
                          "dur_s": 600})
    con.close()
    assert client.post("/api/episodize").json()["created"] == 1
    t = client.get("/api/timeline").json()
    assert len(t["episodes"]) == 1 and len(t["events"]) == 1
    b = client.get("/api/brief").json()
    assert "Brief for" in b["brief"]


def test_api_memory_get_and_delete(client):
    con = db.connect()
    ep_id = _ep(con, "unique quixotic search term")
    con.close()
    assert client.get("/api/memory").json()["episodes"][0]["id"] == ep_id
    hits = client.get("/api/memory", params={"q": "what's quixotic?!"}).json()
    assert [e["id"] for e in hits["episodes"]] == [ep_id]

    assert client.delete(f"/api/memory/{ep_id}").json() == {"deleted": ep_id}
    assert client.get("/api/memory",
                      params={"q": "quixotic"}).json()["episodes"] == []
    assert client.delete(f"/api/memory/{ep_id}").status_code == 404


def test_api_egress(client):
    j = client.get("/api/egress").json()
    assert j["rows"] == []  # zero egress, proven
    assert "127.0.0.1" in j["explanation"]


def test_api_config_get_post(client):
    j = client.get("/api/config").json()
    assert j["config"]["window_sensor"] is True
    j2 = client.post("/api/config", json={
        "window_sensor": False, "watched_folders": ["C:/Users/x/docs"],
        "evil_key": "ignored"}).json()
    assert j2["config"]["window_sensor"] is False
    assert j2["config"]["watched_folders"] == ["C:/Users/x/docs"]
    assert "evil_key" not in j2["config"]
    assert "restart" in j2["note"].lower()
    assert config.load()["window_sensor"] is False  # persisted


# ---------- regressions ----------

def test_prompt_permits_general_questions():
    """Both prompts once said "answer using ONLY the context", so even a
    frontier model refused questions the activity log cannot cover."""
    sections = [("Relevant past activity", ["[episode 1] worked on billing"])]
    for web in (False, True):
        low = memory.build_prompt("how do I timestamp a row in Sheets?",
                                  sections, web=web).lower()
        assert "general" in low, web
        assert "your own knowledge" in low, web


def test_uncited_answer_claims_no_sources(con, monkeypatch):
    """An answer citing nothing used to report every retrieved episode as its
    source — false provenance in a system built on provenance."""
    _ep(con, "worked on billing.py in Code.exe")
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete",
                        lambda *a, **k: "Use an Apps Script onEdit() trigger.")
    assert memory.answer(con, "how do I timestamp a row in Sheets?")["sources"] == []


def test_cited_answer_keeps_its_source(con, monkeypatch):
    ep = _ep(con, "worked on billing.py in Code.exe")
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete",
                        lambda *a, **k: "You were on billing [episode %d]." % ep)
    # query must share keywords with the summary — retrieval is FTS, so
    # "what was I doing?" matches nothing and there'd be no source to keep
    assert memory.answer(con, "billing")["sources"] == [ep]


def test_day_start_survives_after_midnight():
    """At 00:30 a strict calendar day blanks the Today view of the session the
    user is still in the middle of."""
    lt = time.localtime()
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    assert memory.day_start(midnight + 1800) < midnight     # 00:30 reaches back
    assert memory.day_start(midnight + 15 * 3600) == midnight  # 15:00 does not
