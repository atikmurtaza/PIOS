"""Privacy Gate scrubber + cloud-allowed checks."""
from pios import gate


def test_email_redacted_and_consistent():
    clean, labels = gate.scrub("mail me at rafat@gmail.com or rafat@gmail.com")
    assert "gmail.com" not in clean
    assert clean.count("[email-1]") == 2  # same address -> same placeholder
    assert any("email" in l for l in labels)


def test_credentials_redacted():
    for secret in ["sk-abcdef0123456789ABCDEF", "ghp_" + "a" * 30,
                   "AKIAABCDEFGHIJKLMNOP"]:
        clean, labels = gate.scrub("token is " + secret)
        assert secret not in clean
        assert labels


def test_generic_long_token_needs_letter_and_digit():
    # 40-char run with both letters and digits -> redacted
    tok = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0"
    clean, _ = gate.scrub("key=" + tok)
    assert tok not in clean
    # a long all-letter word is NOT a credential
    word = "supercalifragilisticexpialidociousandthensome"
    clean2, _ = gate.scrub(word)
    assert word in clean2


def test_user_path_keeps_tail():
    clean, labels = gate.scrub(r"file at C:\Users\atikm\Projects\PIOS\pios.db")
    assert "atikm" not in clean
    assert "Projects" in clean and "pios.db" in clean
    assert "user path" in labels


def test_timestamp_and_id_not_touched():
    clean, labels = gate.scrub("event 1784510693 in episode row 42")
    assert "1784510693" in clean
    assert not labels


def test_dates_survive_scrubbing():
    """Dates share the phone shape but carry the meaning of an activity log."""
    for d in ["2026-07-20", "20/07/2026", "2026/7/9"]:
        clean, labels = gate.scrub("Mon %s: worked on billing" % d)
        assert d in clean, (d, clean)
        assert not any("phone" in l for l in labels)


def test_real_phone_still_redacted():
    clean, labels = gate.scrub("call me on +44 7700 900123")
    assert "900123" not in clean
    assert any("phone" in l for l in labels)


def test_localhost_not_redacted():
    clean, _ = gate.scrub("server at 127.0.0.1 and 0.0.0.0")
    assert "127.0.0.1" in clean and "0.0.0.0" in clean


def test_public_ip_redacted():
    clean, labels = gate.scrub("connected to 8.8.8.8")
    assert "8.8.8.8" not in clean
    assert any("IP" in l for l in labels)


def test_idempotent():
    once, _ = gate.scrub("email a@b.com key sk-abcdef0123456789ABCDEF")
    twice, _ = gate.scrub(once)
    assert once == twice


def test_cloud_allowed(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert gate.cloud_allowed({"cloud_enabled": False})[0] is False
    ok, reason = gate.cloud_allowed({"cloud_enabled": True})
    assert ok is False and "API key" in reason
    # paid key alone is NOT enough — paid_apis must be explicitly on
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert gate.cloud_allowed({"cloud_enabled": True})[0] is False
    ok, reason = gate.cloud_allowed({"cloud_enabled": True, "paid_apis": True})
    assert ok is True and "anthropic" in reason
    # free tier needs only its key
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    ok, reason = gate.cloud_allowed({"cloud_enabled": True})
    assert ok is True and "gemini" in reason


def test_provider_order_cheapest_first(monkeypatch):
    from pios import cloud
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    assert cloud.providers({}) == ["gemini"]  # paid gated off by default
    assert cloud.providers({"paid_apis": True}) == ["gemini", "anthropic", "openai"]
