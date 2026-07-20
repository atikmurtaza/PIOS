"""PIOS cloud adapters. Dev-phase routing order (Amendment A): cheapest first.

    gemini (free tier)  ->  anthropic (paid)  ->  openai (paid)

Paid providers are attempted ONLY when cfg['paid_apis'] is True — during
development PIOS must never depend on paid APIs. When no provider is usable
the caller falls back to the Manual Cloud Assistant (memory._assist_payload).

stdlib urllib only — a deliberate deviation from "use the official SDK". PIOS's
privacy invariant is a single auditable egress choke point with zero extra deps;
provider SDKs would create network paths the Privacy Gate doesn't sit in front
of. Keys come from env vars only, never stored or logged.

complete() never raises; it returns (text|None, provider|None, error|None).
`error` is a short, safe-to-log reason (no key material).
"""
import json
import os
import urllib.error
import urllib.request

from . import config


def _post(url, headers, body, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_error_detail(e):
    """Best-effort short message from a provider's error body. Never raises."""
    try:
        body = json.loads(e.read().decode("utf-8"))
        msg = body.get("error", {}).get("message") or str(body.get("error"))
        return "HTTP %s: %s" % (e.code, (msg or "")[:200])
    except Exception:
        return "HTTP %s" % e.code


def _gemini_key():
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def providers(cfg):
    """Usable providers in cost order: free first, paid only if enabled."""
    out = []
    if _gemini_key():
        out.append("gemini")
    if cfg.get("paid_apis"):
        if os.environ.get("ANTHROPIC_API_KEY"):
            out.append("anthropic")
        if os.environ.get("OPENAI_API_KEY"):
            out.append("openai")
    return out


# Free-tier models are frequently busy (503) or quota-capped (429). Falling
# through to a sibling model is far cheaper than failing the user's question.
GEMINI_FALLBACKS = ["gemini-3.1-flash-lite", "gemini-flash-lite-latest",
                    "gemini-3-flash-preview"]


def _call_gemini(prompt, system, cfg, timeout):
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    chain = [cfg.get("gemini_model", "gemini-3.5-flash")]
    chain += [m for m in GEMINI_FALLBACKS if m != chain[0]]
    last = None
    for model in chain:
        try:
            data = _post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "%s:generateContent?key=%s" % (model, _gemini_key()),
                {}, body, timeout)
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
            if text:
                return text
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 503):   # 404/400: don't retry siblings
                raise
            last = e
    if last:
        raise last
    return ""


def _call_anthropic(prompt, system, cfg, timeout):
    body = {"model": cfg.get("anthropic_model", "claude-opus-4-8"),
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    data = _post("https://api.anthropic.com/v1/messages",
                 {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                  "anthropic-version": "2023-06-01"}, body, timeout)
    if data.get("stop_reason") == "refusal":
        return ""
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")


def _call_openai(prompt, system, cfg, timeout):
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    data = _post("https://api.openai.com/v1/chat/completions",
                 {"authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                 {"model": cfg.get("openai_model", "gpt-4o"),
                  "messages": msgs}, timeout)
    return data["choices"][0]["message"]["content"]


_CALLS = {"gemini": _call_gemini, "anthropic": _call_anthropic,
          "openai": _call_openai}


def complete(prompt, system=None, timeout=90, cfg=None):
    """Try providers cheapest-first; first success wins.

    Returns (text, provider_used, error). On total failure the error string
    chains every provider's reason so the egress log tells the whole story.
    """
    cfg = cfg if cfg is not None else config.load()
    provs = providers(cfg)
    if not provs:
        return None, None, "no usable provider"
    errors = []
    for prov in provs:
        try:
            text = _CALLS[prov](prompt, system, cfg, timeout)
            if text:
                return text, prov, None
            errors.append("%s: empty/refused response" % prov)
        except urllib.error.HTTPError as e:
            errors.append("%s: %s" % (prov, _http_error_detail(e)))
        except Exception as e:
            errors.append("%s: %s: %s" % (prov, type(e).__name__, e))
    return None, provs[-1], "; ".join(errors)
