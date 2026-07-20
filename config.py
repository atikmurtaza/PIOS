"""PIOS config: load/save %LOCALAPPDATA%\\PIOS\\config.json merged over DEFAULTS."""
import json
import os

DEFAULTS = {'watched_folders': [], 'window_sensor': True, 'file_sensor': True,
            'git_repos': [], 'git_sensor': True,
            'poll_seconds': 3.0, 'idle_seconds': 120, 'model': 'gemma3:4b',
            'cloud_enabled': False, 'anthropic_model': 'claude-opus-4-8',
            # gemini-2.5-flash and the 2.0 line are retired/quota-zeroed for
            # new keys; 3.5-flash verified working on the free tier 2026-07-20.
            'openai_model': 'gpt-4o', 'gemini_model': 'gemini-3.5-flash',
            # Amendment A: dev mode never requires paid APIs; manual web
            # assist is always offered as fallback. Paid needs explicit opt-in.
            'dev_mode': True, 'paid_apis': False}


def _path() -> str:
    # PIOS_CONFIG env override exists purely for tests (spec allows tmp dirs for tests)
    override = os.environ.get('PIOS_CONFIG')
    if override:
        return override
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'PIOS', 'config.json')


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(_path(), encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except (OSError, ValueError):
        pass  # missing or corrupt file -> defaults
    return cfg


def save(cfg: dict) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
