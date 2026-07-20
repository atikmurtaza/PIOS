"""Browser sensor ingest: the one endpoint any web page the user visits can reach."""
import os
import tempfile

# Same bootstrap as test_assist.py: throwaway dirs BEFORE importing pios.
_tmp = tempfile.mkdtemp(prefix="pios-test-")
os.environ.setdefault("PIOS_DB", os.path.join(_tmp, "boot.db"))
os.environ.setdefault("PIOS_CONFIG", os.path.join(_tmp, "boot-config.json"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pios import api, config, db, llm  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PIOS_DB", str(tmp_path / "pios.db"))
    monkeypatch.setenv("PIOS_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(llm, "available", lambda: False)
    monkeypatch.setattr(llm, "complete", lambda *a, **k: None)


@pytest.fixture
def client():
    with TestClient(api.app) as c:
        yield c


@pytest.fixture
def token():
    return api.browser_token()


def post(client, token, events):
    return client.post("/api/events/browser",
                       json={"token": token, "events": events})


def events():
    con = db.connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM events WHERE source='browser' ORDER BY id").fetchall()]
    finally:
        con.close()


def test_token_is_generated_once_and_persisted():
    first = api.browser_token()
    assert len(first) > 20
    assert api.browser_token() == first
    assert config.load()["browser_token"] == first


def test_rejects_missing_and_wrong_token(client, token):
    hit = [{"url": "https://evil.example/", "title": "pwn", "dur_s": 60}]
    assert client.post("/api/events/browser", json={"events": hit}).status_code == 401
    assert post(client, "not-the-token", hit).status_code == 401
    assert events() == []


def test_valid_event_stored_with_hostname_as_app(client, token):
    r = post(client, token, [{"url": "https://github.com/me/pios/pull/7",
                              "title": "Add browser sensor by me · Pull Request #7",
                              "dur_s": 412}])
    assert r.status_code == 200 and r.json()["stored"] == 1
    (ev,) = events()
    assert ev["source"] == "browser"
    assert ev["app"] == "github.com"
    assert ev["title"].startswith("Add browser sensor")
    assert ev["detail"] == "https://github.com/me/pios/pull/7"
    assert ev["dur_s"] == 412


def test_www_stripped_so_time_aggregates(client, token):
    post(client, token, [{"url": "https://www.bbc.co.uk/news", "title": "News",
                          "dur_s": 30}])
    assert events()[0]["app"] == "bbc.co.uk"


def test_blocked_domain_rejected_server_side(client, token):
    # The extension is supposed to filter these; the server must not trust it.
    r = post(client, token, [
        {"url": "https://chase.com/login", "title": "Sign in", "dur_s": 60},
        {"url": "https://secure.chase.com/x", "title": "Bank", "dur_s": 60},
        {"url": "https://news.ycombinator.com/", "title": "HN", "dur_s": 60},
    ])
    assert r.json() == {"stored": 1, "skipped": 2}
    assert [e["app"] for e in events()] == ["news.ycombinator.com"]


def test_non_http_schemes_dropped(client, token):
    r = post(client, token, [{"url": "file:///C:/secrets.txt", "title": "x", "dur_s": 9},
                             {"url": "edge://settings", "title": "y", "dur_s": 9}])
    assert r.json()["stored"] == 0 and events() == []


def test_sensor_off_drops_everything(client, token):
    config.save({**config.load(), "browser_sensor": False})
    r = post(client, token, [{"url": "https://github.com/", "title": "gh", "dur_s": 5}])
    assert r.json()["stored"] == 0 and events() == []


def test_duration_clamped(client, token):
    post(client, token, [{"url": "https://a.com/", "title": "slept", "dur_s": 72000},
                         {"url": "https://b.com/", "title": "negative", "dur_s": -5}])
    assert [e["dur_s"] for e in events()] == [api.MAX_VISIT_S, 0]


def test_batch_and_field_limits(client, token):
    big = [{"url": "https://a.com/%d" % i, "title": "t", "dur_s": 1}
           for i in range(api.MAX_BROWSER_BATCH + 1)]
    assert post(client, token, big).status_code == 413
    # oversized single fields are a validation error, not a stored event
    assert post(client, token, [{"url": "https://a.com/" + "x" * 3000,
                                 "title": "t", "dur_s": 1}]).status_code == 422
    assert post(client, token, [{"url": "https://a.com/", "title": "x" * 600,
                                 "dur_s": 1}]).status_code == 422
    assert events() == []


def test_oversized_body_rejected_before_parsing(client, token):
    r = client.post("/api/events/browser", content=b"{}",
                    headers={"Content-Type": "application/json",
                             "Content-Length": str(api.MAX_BROWSER_BODY + 1)})
    assert r.status_code == 413


def test_extension_config_exposes_blocklist_without_token(client):
    c = client.get("/api/extension/config").json()
    assert "chase.com" in c["blocked_domains"]
    assert c["browser_sensor"] is True
    assert "browser_token" not in c  # the open endpoint must not leak the secret
