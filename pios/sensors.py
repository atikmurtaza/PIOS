"""PIOS sensors: active-window sensor (ctypes Win32) + folder watcher.

Signals, not surveillance: window titles, process names, file paths only.
No screenshots, no keylogging, no window contents, no network access.
Sensors emit plain event dicts through a callback; storage belongs to db.py.

Run `python pios/sensors.py` for a 10-second live demo of the window sensor.
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time
from typing import Callable

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
# Explicit types: HWND/HANDLE are pointer-sized; default c_int restype truncates on 64-bit.
_user32.GetForegroundWindow.restype = wt.HWND
_user32.GetWindowTextLengthW.argtypes = [wt.HWND]
_user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_kernel32.OpenProcess.restype = wt.HANDLE
_kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                                 ctypes.POINTER(wt.DWORD)]
_kernel32.CloseHandle.argtypes = [wt.HANDLE]
_kernel32.GetTickCount64.restype = ctypes.c_ulonglong


def _log(msg: str) -> None:
    print(f'[pios.sensors] {msg}', file=sys.stderr)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.UINT), ('dwTime', wt.DWORD)]


def _idle_seconds() -> float:
    """Seconds since last keyboard/mouse input (GetLastInputInfo)."""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not _user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    # dwTime is a 32-bit tick; mask GetTickCount64 diff to survive the 49-day wrap
    return ((_kernel32.GetTickCount64() - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def _foreground() -> tuple[int, str, str]:
    """(hwnd, app_basename, window_title) of the foreground window; 0/'' on failure."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return 0, '', ''
    n = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    title = buf.value
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app = ''
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if handle:
        try:
            size = wt.DWORD(32768)
            pbuf = ctypes.create_unicode_buffer(size.value)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, pbuf, ctypes.byref(size)):
                app = os.path.basename(pbuf.value)
        finally:
            _kernel32.CloseHandle(handle)
    return hwnd, app, title


class WindowSensor:
    """Polls the foreground window; emits a completed span on every focus change.

    Event: {'source':'window','app','title','ts','dur_s'} where dur_s is how long
    that (app, title) held focus. Title change within the same app counts as a
    change; exact repeats are deduped. Idle > idle_s finalizes the current span
    and suspends emission until input resumes.
    """

    def __init__(self, emit: Callable[[dict], None], poll_s: float = 3.0,
                 idle_s: float = 120, config: dict | None = None):
        self.emit = emit
        self.poll_s = poll_s
        self.idle_s = idle_s
        self.config = config or {}   # only read for opt-in UIA enrichment
        self._stop = threading.Event()
        self._thread = None
        self._cur = None        # (app, title) currently focused, or None
        self._cur_since = 0.0
        self._cur_detail = ''   # UIA enrichment captured when focus was gained

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name='pios-window-sensor',
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.poll_s + 1)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self._tick(time.time())
            except Exception as e:  # sensor thread must never die
                _log(f'window sensor error: {e!r}')

    def _tick(self, now: float) -> None:
        if _idle_seconds() > self.idle_s:
            self._finalize(now)  # close the span; emit nothing while idle
            return
        hwnd, app, title = _foreground()
        if not title or 'PIOS' in title:  # skip empty titles and our own UI
            return
        key = (app, title)
        if key == self._cur:  # dedupe exact repeats
            return
        self._finalize(now)   # emit the previous window's span with its duration
        self._cur = key
        self._cur_since = now
        self._cur_detail = self._enrich(hwnd, app, title)

    def _enrich(self, hwnd: int, app: str, title: str) -> str:
        """Opt-in UIA text for the window just focused. Off/failing == ''."""
        try:
            from . import uia
            return uia.detail_for(hwnd, app, title, self.config)
        except Exception as e:  # enrichment is never allowed to cost us an event
            _log(f'uia enrich failed: {e!r}')
            return ''

    def _finalize(self, now: float) -> None:
        if self._cur:
            app, title = self._cur
            ev = {'source': 'window', 'app': app, 'title': title,
                  'ts': now, 'dur_s': round(now - self._cur_since, 1)}
            if self._cur_detail:  # absent entirely when enrichment is off
                ev['detail'] = self._cur_detail
            self.emit(ev)
        self._cur = None
        self._cur_detail = ''


class FolderSensor:
    """watchdog Observer over the configured folders (recursive).

    Event: {'source':'file','app':'fs','title':'modified README.md',
            'detail': full_path, 'ts':...}. Same path within 30s emits once.
    """
    IGNORE = {'.git', 'node_modules', '.venv', '__pycache__'}

    def __init__(self, emit: Callable[[dict], None], folders: list,
                 debounce_s: float = 30):
        self.emit = emit
        self.folders = [f for f in folders if os.path.isdir(f)]
        self.debounce_s = debounce_s
        self._observer = None
        self._recent = {}  # path -> last emit ts
        self._lock = threading.Lock()

    def start(self) -> None:
        # lazy import: module must import fine without watchdog installed
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        sensor = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                try:
                    sensor._handle(event)
                except Exception as e:  # never crash the observer thread
                    _log(f'folder sensor error: {e!r}')

        self._observer = Observer()
        self._observer.daemon = True
        handler = _Handler()
        for folder in self.folders:
            self._observer.schedule(handler, folder, recursive=True)
        self._observer.start()

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)

    def _handle(self, event) -> None:
        if event.is_directory:
            return
        path = str(event.src_path)
        if self.IGNORE & set(path.replace('\\', '/').split('/')):
            return
        now = time.time()
        with self._lock:
            if now - self._recent.get(path, 0) < self.debounce_s:
                return
            self._recent[path] = now
            if len(self._recent) > 4096:  # ponytail: naive prune; LRU if it matters
                self._recent = {p: t for p, t in self._recent.items()
                                if now - t < self.debounce_s}
        self.emit({'source': 'file', 'app': 'fs',
                   'title': f'{event.event_type} {os.path.basename(path)}',
                   'detail': path, 'ts': now})


def start_all(config: dict, emit: Callable[[dict], None]) -> list:
    """Start configured sensors; return the started instances (each has .stop())."""
    started = []
    if config.get('window_sensor', True):
        ws = WindowSensor(emit, poll_s=config.get('poll_seconds', 3.0),
                          idle_s=config.get('idle_seconds', 120), config=config)
        ws.start()
        started.append(ws)
    folders = config.get('watched_folders') or []
    if config.get('file_sensor', True):
        if not folders:
            _log('folder sensor skipped: no watched_folders configured')
        else:
            try:
                fs = FolderSensor(emit, folders)
                fs.start()
                started.append(fs)
            except ImportError:
                _log('folder sensor skipped: watchdog not installed')
            except Exception as e:
                _log(f'folder sensor failed to start: {e!r}')
    repos = config.get('git_repos') or []
    if config.get('git_sensor', True) and repos:
        from .gitsensor import GitSensor
        gs = GitSensor(emit, repos)
        gs.start()
        started.append(gs)
    return started


if __name__ == '__main__':  # 10-second live demo: watch the foreground window
    print('live check: switch windows for ~10s...', file=sys.stderr)
    s = WindowSensor(print, poll_s=1.0)
    s.start()
    time.sleep(10)
    s.stop()
