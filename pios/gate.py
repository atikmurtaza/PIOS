"""PIOS Privacy Gate. Deterministic scrubber for outbound cloud payloads.

No network, no LLM, stdlib only. scrub() replaces sensitive spans with stable
bracketed placeholders and returns a human-readable label per unique redaction
(masked) so the UI can show the user exactly what was hidden before anything
leaves the machine. Order matters: credentials before the generic long-run
rule, emails before phones.
"""
import os
import re

# credential shapes, checked before the generic long-token rule
_CRED_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{16,}",
    r"ghp_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"xox[bap]-[A-Za-z0-9-]{10,}",
    r"AKIA[0-9A-Z]{16}",
    r"ntn_[A-Za-z0-9]{20,}",
    r"whsec_[A-Za-z0-9]{20,}",
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",  # JWT
]
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CRED = re.compile("|".join(_CRED_PATTERNS))
# generic secret: 32+ unbroken run with at least one letter AND one digit
_LONGTOK = re.compile(r"\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{32,}\b")
_USERPATH = re.compile(r"([A-Za-z]:\\Users\\)[^\\/:*?\"<>|\r\n]+", re.IGNORECASE)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# phone: needs a + prefix or an internal separator, so unix timestamps are safe
_PHONE = re.compile(r"(?<![\w.])(?:\+\d[\d ().-]{8,14}\d|\d{2,4}[ .-]\d{2,4}[ .-]\d{2,6})(?![\w.])")
# ...but dates share that shape. Redacting them blinds the model to *when*
# things happened, which is most of the value of an activity log.
_DATEISH = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$|^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")
_SAFE_IPS = {"127.0.0.1", "0.0.0.0"}


def _mask(v):
    return v[0] + "***" + v[-1] if len(v) > 2 else "***"


def scrub(text):
    """Return (clean_text, labels). Idempotent."""
    if not text:
        return text, []
    labels = []
    counters = {}
    seen = {}  # original value -> placeholder (stable within one scrub)

    def placeholder(kind, value, label_fn):
        if value in seen:
            return seen[value]
        counters[kind] = counters.get(kind, 0) + 1
        ph = "[%s-%d]" % (kind, counters[kind])
        seen[value] = ph
        labels.append(label_fn(value))
        return ph

    def sub(pattern, kind, label_fn):
        return pattern.sub(
            lambda m: placeholder(kind, m.group(0), label_fn), text)

    # user paths keep the tail (useful context), only the name is replaced
    def _path_repl(m):
        labels.append("user path")
        return m.group(1) + "[user]"
    text = _USERPATH.sub(_path_repl, text)

    text = sub(_EMAIL, "email", lambda v: "email: " + _mask(v))
    text = sub(_CRED, "token", lambda v: "credential token: " + _mask(v))
    text = sub(_LONGTOK, "token", lambda v: "credential-like token: " + _mask(v))
    text = _PHONE.sub(
        lambda m: (m.group(0) if _DATEISH.match(m.group(0).strip())
                   else placeholder("phone", m.group(0),
                                    lambda v: "phone: " + _mask(v))),
        text)
    text = _IPV4.sub(
        lambda m: (m.group(0) if m.group(0) in _SAFE_IPS
                   else placeholder("ip", m.group(0), lambda v: "IP: " + _mask(v))),
        text)
    # dedupe path label if it fired more than once
    if labels.count("user path") > 1:
        labels = [x for x in labels if x != "user path"] + ["user path"]
    return text, labels


def cloud_allowed(cfg):
    """(bool, reason) — is an automatic cloud API call permitted right now?

    Dev-phase policy (Amendment A): free tier (Gemini) needs only its key;
    paid providers additionally need the explicit 'paid_apis' setting. When
    this returns False the caller offers the Manual Cloud Assistant instead.
    """
    if not cfg.get("cloud_enabled"):
        return False, "cloud is disabled in settings"
    from . import cloud  # no cycle: cloud imports only config
    provs = cloud.providers(cfg)
    if not provs:
        return False, ("no usable API key — free tier: set GEMINI_API_KEY; "
                       "paid: turn on 'Allow paid APIs' and set "
                       "ANTHROPIC_API_KEY or OPENAI_API_KEY")
    return True, "ok (%s)" % " -> ".join(provs)
