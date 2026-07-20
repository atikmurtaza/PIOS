"""UIA enrichment: consent gating, crash/slowness containment, caching.

Everything here runs headless -- the COM layer is monkeypatched out. The one
test that genuinely needs a live desktop skips itself.
"""
import sys
import time

import pytest

from pios import sensors, uia

ON = {'uia_enrich': True, 'uia_apps': ['msedge.exe']}


@pytest.fixture(autouse=True)
def clear_cache():
    uia._cache.clear()
    yield
    uia._cache.clear()


@pytest.fixture
def fake_extract(monkeypatch):
    """Replace the COM layer; record how often it was actually called."""
    calls = []

    def fake(hwnd, app):
        calls.append((hwnd, app))
        return {'url': 'https://example.com/x'}

    monkeypatch.setattr(uia, '_extract', fake)
    return calls


# --- consent gate -----------------------------------------------------------

def test_skipped_when_disabled(fake_extract):
    cfg = {'uia_enrich': False, 'uia_apps': ['msedge.exe']}
    assert uia.enrich(1, 'msedge.exe', 'Docs', cfg) is None
    assert fake_extract == []


def test_skipped_when_app_not_opted_in(fake_extract):
    assert uia.enrich(1, 'code.exe', 'main.py', ON) is None
    assert fake_extract == []


def test_defaults_enrich_nothing(fake_extract):
    from pios import config
    assert config.DEFAULTS['uia_enrich'] is False
    assert config.DEFAULTS['uia_apps'] == []
    assert uia.enrich(1, 'msedge.exe', 'Docs', config.DEFAULTS) is None
    assert fake_extract == []


def test_runs_when_opted_in(fake_extract):
    assert uia.enrich(1, 'msedge.exe', 'Docs', ON) == {'url': 'https://example.com/x'}
    assert len(fake_extract) == 1


@pytest.mark.parametrize('title', [
    'Sign in - Google Accounts', 'Enter your password', 'Bitwarden',
    '1Password - Vault', 'LastPass', 'Chase Banking', 'Login | Acme',
    'Authenticator setup', 'SIGN IN',  # case-insensitive
])
def test_credential_titles_are_never_enriched(fake_extract, title):
    assert uia.enrich(1, 'msedge.exe', title, ON) is None
    assert fake_extract == []


def test_app_match_is_case_insensitive(fake_extract):
    assert uia.enrich(1, 'MsEdge.EXE', 'Docs', ON) is not None


# --- containment: slow / throwing extractor -------------------------------

def test_slow_extractor_times_out_and_returns_none(monkeypatch):
    monkeypatch.setattr(uia, 'TIMEOUT_S', 0.2)
    monkeypatch.setattr(uia, '_extract', lambda h, a: time.sleep(30))
    t0 = time.perf_counter()
    assert uia.enrich(1, 'msedge.exe', 'Docs', ON) is None
    assert time.perf_counter() - t0 < 1.0


def test_throwing_extractor_returns_none(monkeypatch):
    def boom(hwnd, app):
        raise RuntimeError('COM said no')

    monkeypatch.setattr(uia, '_extract', boom)
    assert uia.enrich(1, 'msedge.exe', 'Docs', ON) is None


def test_slow_extractor_does_not_delay_or_break_event(monkeypatch):
    """The whole point: enrichment problems cost signal, never the event."""
    monkeypatch.setattr(uia, 'TIMEOUT_S', 0.2)
    monkeypatch.setattr(uia, '_extract', lambda h, a: time.sleep(30))
    monkeypatch.setattr(sensors, '_idle_seconds', lambda: 0.0)
    monkeypatch.setattr(sensors, '_foreground', lambda: (1, 'msedge.exe', 'Docs'))

    events = []
    ws = sensors.WindowSensor(events.append, config=ON)
    t0 = time.perf_counter()
    ws._tick(1000.0)                                    # gains focus (slow enrich)
    monkeypatch.setattr(sensors, '_foreground', lambda: (2, 'code.exe', 'main.py'))
    ws._tick(1060.0)                                    # focus change -> emit span
    elapsed = time.perf_counter() - t0

    assert elapsed < 2.0, 'sensor thread was stalled by enrichment'
    assert events == [{'source': 'window', 'app': 'msedge.exe', 'title': 'Docs',
                       'ts': 1060.0, 'dur_s': 60.0}]   # plain event, no detail key


def test_throwing_enrichment_does_not_break_event(monkeypatch):
    def boom(*a):
        raise RuntimeError('nope')

    monkeypatch.setattr(uia, 'detail_for', boom)
    monkeypatch.setattr(sensors, '_idle_seconds', lambda: 0.0)
    monkeypatch.setattr(sensors, '_foreground', lambda: (1, 'msedge.exe', 'Docs'))
    events = []
    ws = sensors.WindowSensor(events.append, config=ON)
    ws._tick(1000.0)
    monkeypatch.setattr(sensors, '_foreground', lambda: (2, 'code.exe', 'main.py'))
    ws._tick(1010.0)
    assert events[0]['title'] == 'Docs' and 'detail' not in events[0]


# --- cache ------------------------------------------------------------------

def test_cache_prevents_repeat_extraction(fake_extract):
    for _ in range(5):
        assert uia.enrich(7, 'msedge.exe', 'Docs', ON)['url'] == 'https://example.com/x'
    assert len(fake_extract) == 1


def test_cache_key_includes_title(fake_extract):
    uia.enrich(7, 'msedge.exe', 'Docs', ON)
    uia.enrich(7, 'msedge.exe', 'Other page', ON)      # same window, new title
    assert len(fake_extract) == 2


def test_failures_are_cached_too(monkeypatch):
    calls = []
    monkeypatch.setattr(uia, '_extract', lambda h, a: calls.append(1))
    for _ in range(3):
        assert uia.enrich(7, 'msedge.exe', 'Docs', ON) is None
    assert len(calls) == 1, 'a wedged window must not be retried every poll'


# --- event shape ------------------------------------------------------------

def test_enrichment_lands_in_detail_and_title_stays_plain(monkeypatch):
    monkeypatch.setattr(uia, '_extract', lambda h, a: {'url': 'https://ex.com/a'})
    monkeypatch.setattr(sensors, '_idle_seconds', lambda: 0.0)
    monkeypatch.setattr(sensors, '_foreground', lambda: (1, 'msedge.exe', 'Docs'))
    events = []
    ws = sensors.WindowSensor(events.append, config=ON)
    ws._tick(1000.0)
    monkeypatch.setattr(sensors, '_foreground', lambda: (2, 'code.exe', 'main.py'))
    ws._tick(1030.0)
    assert events[0] == {'source': 'window', 'app': 'msedge.exe', 'title': 'Docs',
                         'ts': 1030.0, 'dur_s': 30.0, 'detail': 'url=https://ex.com/a'}


def test_disabled_is_byte_identical_to_before(monkeypatch):
    monkeypatch.setattr(sensors, '_idle_seconds', lambda: 0.0)
    monkeypatch.setattr(sensors, '_foreground', lambda: (1, 'msedge.exe', 'Docs'))
    events = []
    ws = sensors.WindowSensor(events.append)            # no config at all
    ws._tick(1000.0)
    monkeypatch.setattr(sensors, '_foreground', lambda: (2, 'code.exe', 'main.py'))
    ws._tick(1030.0)
    assert events == [{'source': 'window', 'app': 'msedge.exe', 'title': 'Docs',
                       'ts': 1030.0, 'dur_s': 30.0}]


def test_url_heuristic():
    assert uia._looks_like_url('https://a.dev/x') and uia._looks_like_url('a.dev/x')
    assert not uia._looks_like_url('how to cook rice')
    assert not uia._looks_like_url('')


def test_long_values_are_truncated(monkeypatch):
    monkeypatch.setattr(uia, '_extract', lambda h, a: {'doc': 'x' * 5000})
    d = uia.detail_for(1, 'msedge.exe', 'Docs', ON)
    assert len(d) < uia.MAX_LEN + 20, 'never a document body'


def test_background_window_never_reads_the_focused_window(monkeypatch):
    """GetFocusedElement is global. Enriching a NON-foreground window must not
    return the foreground window's text -- wrong attribution, and it reads a
    window that was never opted into. Regression: File Explorer once reported
    the browser's 'Chat with ChatGPT'.
    """
    monkeypatch.setattr(uia._user32, 'GetForegroundWindow', lambda: 999)
    assert uia._extract(1, 'explorer.exe') is None      # 1 != foreground 999


# --- live desktop (skipped headless) ---------------------------------------

def test_live_extraction_smoke():
    if sys.platform != 'win32':
        pytest.skip('Windows only')
    hwnd, app, title = sensors._foreground()
    if not hwnd or not app:
        pytest.skip('no foreground window (headless/CI)')
    cfg = {'uia_enrich': True, 'uia_apps': [app]}
    t0 = time.perf_counter()
    out = uia.enrich(hwnd, app, title, cfg)
    ms = (time.perf_counter() - t0) * 1000
    assert out is None or isinstance(out, dict)
    assert ms < uia.TIMEOUT_S * 1000 + 300, f'enrichment took {ms:.0f}ms'
