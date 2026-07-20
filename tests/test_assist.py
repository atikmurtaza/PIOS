"""Manual Cloud Assistant (Amendment A): assist payload + paste-back import."""
import os
import tempfile

# Same bootstrap as test_pios.py: point PIOS at throwaway dirs BEFORE import.
_tmp = tempfile.mkdtemp(prefix="pios-test-")
os.environ.setdefault("PIOS_DB", os.path.join(_tmp, "boot.db"))
os.environ.setdefault("PIOS_CONFIG", os.path.join(_tmp, "boot-config.json"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pios import api, config, db, llm, memory  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PIOS_DB", str(tmp_path / "pios.db"))
    monkeypatch.setenv("PIOS_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(llm, "available", lambda: False)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: None)
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        yield c


def test_cloud_chat_without_keys_offers_manual_assist(client):
    config.save({**config.DEFAULTS, "cloud_enabled": True})
    r = client.post("/api/chat",
                    json={"message": "hard question", "cloud": True}).json()
    assert "answered locally" in r["answer"]
    a = r["assist"]
    assert a["question"] == "hard question"
    assert a["prompt"]  # the scrubbed outbound text, ready for the clipboard
    names = [p["name"] for p in a["providers"]]
    assert a["recommended"] in names and "Gemini" in names
    # the prepared prompt is on the egress ledger
    rows = client.get("/api/egress").json()["rows"]
    assert any("manual-assist" in (row["destination"] or "") for row in rows)


def test_local_chat_never_offers_assist(client):
    r = client.post("/api/chat",
                    json={"message": "hi", "cloud": False}).json()
    assert "assist" not in r


def test_assist_import_stores_searchable_memory(client):
    r = client.post("/api/assist/import", json={
        "question": "how do stripe webhooks retry?",
        "response": "Stripe retries webhooks with exponential backoff for 3 days.",
        "provider": "Gemini"}).json()
    assert r["stored"] and r["episode"]
    con = db.connect()
    try:
        hits = db.search(con, "stripe webhooks retry backoff")
        assert any(h["id"] == r["episode"] for h in hits)
        ep = con.execute("SELECT * FROM episodes WHERE id=?",
                         (r["episode"],)).fetchone()
        assert ep["source_event_ids"]  # provenance points at the chat event
        assert "Gemini" in ep["summary"]
    finally:
        con.close()


def test_assist_import_rejects_empty(client):
    resp = client.post("/api/assist/import",
                       json={"question": "q", "response": "   "})
    assert resp.status_code == 400
