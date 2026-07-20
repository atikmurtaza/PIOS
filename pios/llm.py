"""PIOS Ollama adapter. E1-owned. stdlib urllib only; localhost only.

The LLM is an enhancer, never a dependency: complete() returns None on ANY
failure and callers must degrade gracefully.

RAM discipline (this laptop has 16GB): the model is loaded ONLY when the user
asks a question — background jobs never call complete() — and keep_alive=5m
lets Ollama unload it shortly after a chat burst.
"""
import json
import urllib.request

from . import config

BASE = "http://127.0.0.1:11434"  # privacy invariant #1: only egress allowed


def model_name():
    return config.load().get("model", "gemma3:4b")


def available():
    try:
        with urllib.request.urlopen(BASE + "/api/tags", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def complete(prompt, system=None, timeout=180):
    body = {"model": model_name(), "prompt": prompt, "stream": False,
            "keep_alive": "5m"}
    if system:
        body["system"] = system
    req = urllib.request.Request(
        BASE + "/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8")).get("response")
            return resp if isinstance(resp, str) else None
    except Exception:
        return None
