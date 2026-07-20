"""PIOS UI Automation enrichment: the on-screen TEXT of a few named controls.

Why this exists: the window sensor knows "msedge.exe for 62 min" but not what was
on screen. Screenshots + OCR were rejected (cost, inaccuracy, and OCR'd text
carries the same privacy weight as the pixels). UI Automation -- the accessibility
tree screen readers use -- returns the actual text of *specific* controls in
milliseconds, no pixels involved and precise scope control.

What it reads, deliberately narrow:
  browsers  -> the address bar's value (the URL), nothing else. This is the
               fallback for when the browser extension isn't installed.
  other     -> the focused element's *Name* (a label: "main.py", "Inbox").
               Never its Value: a Name identifies what you're looking at, a
               Value on an edit control IS the document body.
Never page bodies, never keystrokes, never pixels.

Consent: OFF unless config['uia_enrich'] is True AND the process is named in
config['uia_apps']. Both are empty by default -- nothing is enriched until the
user names specific apps.

No third-party dependency: raw ctypes against UIAutomationCore's COM vtables.
comtypes/uiautomation were considered and not needed -- the whole surface is 5
methods across 2 interfaces (verified working, incl. VARIANT-by-value on x64).

Run `python -m pios.uia` for a self-check + a live extraction from the window you
focus.
"""
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time

TIMEOUT_S = 0.5      # hard cap: a wedged app must never stall the sensor thread
MAX_LEN = 300        # short identifying strings only, never bodies
_CACHE_MAX = 128

BROWSERS = {'msedge.exe', 'chrome.exe', 'brave.exe', 'firefox.exe', 'opera.exe'}

# Heuristic backstop, NOT a guarantee: a window whose title looks like a
# credential surface is never enriched. Auditable on purpose -- extend it freely.
# The real protection is that enrichment is opt-in per app.
CREDENTIAL_MARKERS = (
    'password', 'passwd', 'sign in', 'signin', 'sign-in', 'log in', 'login',
    'credential', 'authenticator', '2fa', 'two-factor', 'one-time', 'otp',
    'bitwarden', '1password', 'lastpass', 'keeper', 'dashlane', 'keepass',
    'bank', 'banking', 'wallet', 'seed phrase', 'private key', 'secret',
)

_PROP_CONTROLTYPE = 30003
_PROP_NAME = 30005
_PROP_VALUE = 30045
_CTRL_EDIT = 50004
_SCOPE_DESCENDANTS = 4
_VT_I4, _VT_BSTR = 3, 8
_CLSCTX_INPROC_SERVER = 1

_ole32 = ctypes.windll.ole32
_oleaut32 = ctypes.windll.oleaut32
_user32 = ctypes.windll.user32
_user32.GetForegroundWindow.restype = wt.HWND  # pointer-sized; c_int truncates on x64

_cache = {}          # (hwnd, title) -> dict | None
_cache_lock = threading.Lock()


def _log(msg: str) -> None:
    print(f'[pios.uia] {msg}', file=sys.stderr)


class _GUID(ctypes.Structure):
    _fields_ = [('d1', ctypes.c_ulong), ('d2', ctypes.c_ushort),
                ('d3', ctypes.c_ushort), ('d4', ctypes.c_ubyte * 8)]


class _VARIANT(ctypes.Structure):
    # 24 bytes on x64: vt + 3 reserved words + a 16-byte union.
    _fields_ = [('vt', ctypes.c_ushort), ('r1', ctypes.c_ushort),
                ('r2', ctypes.c_ushort), ('r3', ctypes.c_ushort),
                ('val', ctypes.c_longlong), ('val2', ctypes.c_longlong)]


def _guid(s: str) -> _GUID:
    g = _GUID()
    _ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


_CLSID_CUIAutomation = _guid('{ff48dba4-60ef-4201-aa87-54103eef594e}')
_IID_IUIAutomation = _guid('{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}')


def _vcall(ptr, index, *argtypes):
    """Bind vtable slot `index` of a COM interface pointer. HRESULT-returning."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(ptr) -> None:
    if ptr:
        _vcall(ptr, 2)(ptr)  # IUnknown::Release


def _prop_str(element, prop_id: int) -> str:
    """A string property of an element, '' if absent. Frees the BSTR."""
    v = _VARIANT()
    hr = _vcall(element, 10, ctypes.c_int, ctypes.POINTER(_VARIANT))(
        element, prop_id, ctypes.byref(v))          # GetCurrentPropertyValue
    if hr != 0 or v.vt != _VT_BSTR:
        return ''
    try:
        return (ctypes.cast(v.val, ctypes.c_wchar_p).value or '').strip()
    finally:
        _oleaut32.VariantClear(ctypes.byref(v))


def _extract(hwnd: int, app: str) -> dict | None:
    """The actual UIA work. Runs on a throwaway thread; assume it may hang."""
    _ole32.CoInitializeEx(None, 0)                  # ~3ms on a fresh thread
    auto = ctypes.c_void_p()
    element = ctypes.c_void_p()
    cond = ctypes.c_void_p()
    found = ctypes.c_void_p()
    try:
        if _ole32.CoCreateInstance(ctypes.byref(_CLSID_CUIAutomation), None,
                                   _CLSCTX_INPROC_SERVER,
                                   ctypes.byref(_IID_IUIAutomation),
                                   ctypes.byref(auto)) != 0:
            return None
        if _vcall(auto, 6, wt.HWND, ctypes.POINTER(ctypes.c_void_p))(
                auto, hwnd, ctypes.byref(element)) != 0 or not element:
            return None                             # ElementFromHandle

        if app.lower() in BROWSERS:
            cv = _VARIANT()
            cv.vt, cv.val = _VT_I4, _CTRL_EDIT
            if _vcall(auto, 23, ctypes.c_int, _VARIANT,
                      ctypes.POINTER(ctypes.c_void_p))(
                    auto, _PROP_CONTROLTYPE, cv, ctypes.byref(cond)) != 0:
                return None                         # CreatePropertyCondition
            # Targeted FindFirst, not a tree walk: the address bar is the first
            # Edit under the browser frame (~30ms measured on Edge).
            if _vcall(element, 5, ctypes.c_int, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_void_p))(
                    element, _SCOPE_DESCENDANTS, cond, ctypes.byref(found)) != 0:
                return None
            if not found:
                return None
            url = _prop_str(found, _PROP_VALUE)
            return {'url': url[:MAX_LEN]} if _looks_like_url(url) else None

        # Non-browser: the focused control's label. GetFocusedElement is GLOBAL --
        # it returns whatever has focus system-wide, which for a background hwnd
        # is a DIFFERENT window's content (wrong attribution, and a read of a
        # window the user never opted into). Only trust it when this window is
        # actually the foreground one.
        if _user32.GetForegroundWindow() != hwnd:
            return None
        if _vcall(auto, 8, ctypes.POINTER(ctypes.c_void_p))(
                auto, ctypes.byref(found)) != 0 or not found:
            return None
        name = _prop_str(found, _PROP_NAME)
        # >120 chars is a document body wearing a Name, not a label. We want the
        # label that says WHAT is open, never the text that is in it.
        return {'doc': name[:MAX_LEN]} if name and len(name) <= 120 else None
    finally:
        for p in (found, cond, element, auto):
            _release(p)
        _ole32.CoUninitialize()


def _looks_like_url(s: str) -> bool:
    """Address bars also hold half-typed search terms; keep only real URLs."""
    return bool(s) and ' ' not in s and ('.' in s or s.startswith('http'))


def is_credential_title(title: str) -> bool:
    low = (title or '').lower()
    return any(m in low for m in CREDENTIAL_MARKERS)


def allowed(app: str, title: str, config: dict) -> bool:
    """Consent gate: opt-in globally, opt-in per app, never credential surfaces."""
    if not config or not config.get('uia_enrich'):
        return False
    apps = {a.lower() for a in (config.get('uia_apps') or [])}
    if (app or '').lower() not in apps:
        return False
    return not is_credential_title(title)


def enrich(hwnd: int, app: str, title: str, config: dict) -> dict | None:
    """Short identifying text for a window, or None. Never raises, never blocks
    longer than TIMEOUT_S, and costs nothing for a window already seen."""
    if not hwnd or not allowed(app, title, config):
        return None
    key = (hwnd, title)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()  # ponytail: bulk evict; LRU only if this ever shows up
    result = _run_bounded(_extract, hwnd, app)
    with _cache_lock:
        _cache[key] = result
    return result


def _run_bounded(fn, *args):
    """Run fn on a daemon thread, give up after TIMEOUT_S. A wedged UIA call
    leaks one parked thread rather than stalling (or poisoning) the sensor."""
    box = []

    def work():
        try:
            box.append(fn(*args))
        except Exception as e:      # crash-safe: enrichment failure is not an outage
            _log(f'extract failed: {e!r}')

    t = threading.Thread(target=work, daemon=True, name='pios-uia')
    t.start()
    t.join(TIMEOUT_S)
    return box[0] if box else None


def detail_for(hwnd: int, app: str, title: str, config: dict) -> str:
    """Enrichment formatted for the event's `detail` field. '' when off/failed."""
    d = enrich(hwnd, app, title, config)
    if not d:
        return ''
    # Cap here, at the boundary where text becomes an event: the length limit
    # must hold whatever the extractor hands back.
    return '; '.join(f'{k}={str(v)[:MAX_LEN]}' for k, v in d.items() if v)


def _self_check() -> None:
    cfg = {'uia_enrich': True, 'uia_apps': ['msedge.exe']}
    assert not allowed('msedge.exe', 'x', {'uia_enrich': False, 'uia_apps': ['msedge.exe']})
    assert not allowed('code.exe', 'x', cfg)          # not opted in
    assert allowed('MSEDGE.EXE', 'x', cfg)            # case-insensitive
    assert not allowed('msedge.exe', 'Sign in - Google', cfg)
    assert not allowed('msedge.exe', 'My BANKING portal', cfg)
    assert _looks_like_url('https://a.dev/x') and _looks_like_url('a.dev/x')
    assert not _looks_like_url('how to cook rice') and not _looks_like_url('')
    assert enrich(0, 'msedge.exe', 't', cfg) is None  # no hwnd
    assert enrich(1234, 'code.exe', 't', cfg) is None  # gated off
    print('self-check OK')


if __name__ == '__main__':  # python -m pios.uia
    _self_check()
    from .sensors import _foreground  # HWND restype already set there
    print('live check: focus the window you want read, 5s...', file=sys.stderr)
    time.sleep(5)
    h, a, ttl = _foreground()
    cfg = {'uia_enrich': True, 'uia_apps': [a]}
    t0 = time.perf_counter()
    out = enrich(h, a, ttl, cfg)
    print(f'{a} | {ttl}\n  -> {out}  [{(time.perf_counter() - t0) * 1000:.0f}ms]')
