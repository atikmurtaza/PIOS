"""PIOS memory core: episodizer, retrieval, brief, chat answerer. E1-owned.

Deterministic pipelines. The LLM is used ONLY in answer() (user-initiated chat):
background jobs (episodize, brief) are pure heuristics so the model never sits
resident in RAM — keeping a 4-10GB model warm for a nightly-quality summary was
what lagged this 16GB laptop. Every episode stores provenance.
"""
import re
import time
from collections import Counter

from . import db, llm

EPISODIZE_MIN_AGE_S = 15 * 60   # only events older than this
GAP_SPLIT_S = 10 * 60           # split blocks on gaps longer than this


def _midnight(ts):
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def _day(ts):
    return time.strftime("%a %Y-%m-%d", time.localtime(ts))


def _ymd(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


DAY_LOOKBACK_S = 6 * 3600


def day_start(now=None):
    """Start of the visible 'today' window.

    Not simply midnight: someone working at 00:30 is mid-session, and a strict
    calendar day blanks the Today view of everything they just did. Always
    reach back at least DAY_LOOKBACK_S so late-night work stays visible.
    """
    now = now if now is not None else time.time()
    return min(_midnight(now), now - DAY_LOOKBACK_S)


RESUME_GAP_S = 30 * 60          # a >30-min gap ends the current work span

# Both question types reach the same model. It must ground claims about the
# user's activity in the log, but stay a normal, useful assistant for general
# questions the log has nothing to say about.
SYSTEM_PROMPT = (
    "You are the user's personal assistant. You have access to a log of their "
    "computer activity. Ground any claim about what they did in that log and "
    "cite it; answer general questions normally from your own knowledge.")


def _heuristic_summary(block):
    """Truthful one-line summary built ONLY from real event data."""
    app_dur = Counter()
    titles = []
    for r in block:
        app = r["app"] or r["source"]
        # ponytail: events without dur_s count as 60s so file events still rank
        app_dur[app] += r["dur_s"] or 60
        t = _clean_title(r["title"])   # drop tab-counts/profile/"Not Responding"
        if t and t not in titles:
            titles.append(t)
    apps = [a for a, _ in app_dur.most_common()]
    end = block[-1]["ts"] + (block[-1]["dur_s"] or 0)
    span_min = max(1, int(round((end - block[0]["ts"]) / 60)))
    summary = "%s-%s (%d min): %s — %s (%d events)" % (
        _hm(block[0]["ts"]), _hm(end), span_min,
        ", ".join(apps[:3]),
        "; ".join(titles[:3]) or "no titles",
        len(block))
    return summary, apps


def episodize(con, now=None):
    """Group un-episodized window/file events older than 15 min into episodes.

    Blocks split on >10-min gaps. Heuristic summaries only (truthful by
    construction, and never load the model). Returns number of episodes created.
    """
    now = now if now is not None else time.time()
    rows = con.execute(
        "SELECT * FROM events WHERE episodized = 0 "
        "AND source IN ('window','file','git','browser') AND ts < ? ORDER BY ts",
        (now - EPISODIZE_MIN_AGE_S,)).fetchall()
    if not rows:
        return 0

    blocks, block = [], [rows[0]]
    for prev, cur in zip(rows, rows[1:]):
        if cur["ts"] - (prev["ts"] + (prev["dur_s"] or 0)) > GAP_SPLIT_S:
            blocks.append(block)
            block = []
        block.append(cur)
    blocks.append(block)

    made = 0
    for block in blocks:
        summary, apps = _heuristic_summary(block)
        ids = [str(r["id"]) for r in block]
        db.insert_episode(con, {
            "start_ts": block[0]["ts"],
            "end_ts": block[-1]["ts"] + (block[-1]["dur_s"] or 0),
            "summary": summary,
            "apps": ",".join(apps),
            "source_event_ids": ",".join(ids),
        })
        con.execute(
            "UPDATE events SET episodized = 1 WHERE id IN (%s)"
            % ",".join("?" * len(ids)), ids)
        con.commit()
        made += 1
    return made


def _consolidate_day(con, day_ymd, eps):
    """Distill one past day's episodes into facts. Heuristic always; LLM extra
    if available and grounded. Returns number of new facts."""
    ep_ids = ",".join(str(e["id"]) for e in eps)
    day_label = _day(eps[0]["start_ts"])
    app_dur = Counter()
    titles = []
    for e in eps:
        for a in (e["apps"] or "").split(","):
            if a:
                app_dur[a] += 1
        for t in re.split(r"[;—-]", e["summary"]):
            t = t.strip()
            if t and len(t) < 60 and t not in titles:
                titles.append(t)
    top = ", ".join(a for a, _ in app_dur.most_common(3))
    heuristic = "%s: %d work blocks, mostly %s" % (
        day_label, len(eps), top or "misc activity")

    facts = [heuristic]
    if llm.available():
        summaries = "\n".join("- " + e["summary"] for e in eps)
        raw = llm.complete(
            "These are activity-log summaries from one day:\n%s\n\n"
            "List up to 5 short, durable facts about what the user worked on "
            "that day. One per line, no numbering. Use ONLY the summaries; "
            "invent nothing." % summaries,
            system="You distill activity logs into short factual bullet points.",
            timeout=90)
        if raw:
            src_lc = summaries.lower()
            for line in raw.splitlines():
                line = line.strip().lstrip("-*0123456789. ").strip()
                if not line or len(line) > 500:
                    continue
                # groundedness: keep only facts whose key words appear in sources
                words = [w for w in re.findall(r"[A-Za-z]{4,}", line.lower())]
                if words and sum(w in src_lc for w in words) < max(1, len(words) // 2):
                    continue
                facts.append(line)

    made = 0
    for text in facts:
        if con.execute("SELECT 1 FROM facts WHERE day=? AND text=?",
                       (day_ymd, text)).fetchone():
            continue
        db.insert_fact(con, {"day": day_ymd, "text": text,
                             "source_episode_ids": ep_ids})
        made += 1
    return made


def consolidate(con, now=None):
    """Distill episodes from past days into durable facts. Idempotent per day
    via the meta 'last_consolidated_day' watermark. Runs at most once/day/day."""
    now = now if now is not None else time.time()
    today = _ymd(now)
    last = db.meta_get(con, "last_consolidated_day", "")
    # group past-day episodes by their YMD
    by_day = {}
    for e in db.episodes_between(con, 0, _midnight(now)):
        d = _ymd(e["start_ts"])
        if d >= today or d <= last:
            continue
        by_day.setdefault(d, []).append(e)

    made = 0
    for d in sorted(by_day):
        made += _consolidate_day(con, d, by_day[d])
    if by_day:
        db.meta_set(con, "last_consolidated_day", max(by_day))
    return made


def resume(con, now=None):
    """Reconstruct the last contiguous work span. Deterministic, no LLM."""
    now = now if now is not None else time.time()
    rows = con.execute(
        "SELECT * FROM events WHERE source IN ('window','file') "
        "ORDER BY ts DESC LIMIT 500").fetchall()  # newest first
    if not rows:
        return {"summary": "No recorded activity yet.", "events": [],
                "episode_ids": [], "gap_min": None}

    span = [rows[0]]
    for prev, cur in zip(rows, rows[1:]):  # walking backwards in time
        if prev["ts"] - (cur["ts"] + (cur["dur_s"] or 0)) > RESUME_GAP_S:
            break
        span.append(cur)
    span.reverse()  # chronological

    last = span[-1]
    titles = []
    for e in reversed(span):
        t = e["title"] or (e["app"] or e["source"])
        if t and t not in titles:
            titles.append(t)
        if len(titles) >= 3:
            break
    gap_min = int((now - (last["ts"] + (last["dur_s"] or 0))) / 60)
    summary = "You were working on %s until %s (%d min ago)." % (
        "; ".join(titles), _hm(last["ts"]), max(0, gap_min))
    ep_ids = [e["id"] for e in db.episodes_between(
        con, span[0]["ts"], last["ts"] + (last["dur_s"] or 0))]
    return {"summary": summary,
            "events": [dict(e) for e in span[-10:]],
            "episode_ids": ep_ids, "gap_min": max(0, gap_min)}


def retrieve(con, query):
    """Facts + activity episodes + prior Q&A + today's events + where I left off.

    Each item carries a 'type'. Prior Q&A is type 'assist', NOT 'episode': it
    gets its own small budget so it can never crowd observed activity out of
    the episode limit, and callers can frame it as reference rather than as
    something the user did.
    """
    out = [dict(r, type="fact") for r in db.search_facts(con, query)]
    out += [dict(r, type="episode") for r in db.search(con, query, assist=False)]
    out += [dict(r, type="assist")
            for r in db.search(con, query, limit=2, assist=True)]
    out += [dict(r, type="event")
            for r in db.recent_events(con, _midnight(time.time()))]
    res = resume(con)
    if res["events"]:
        out.append({"type": "resume", "summary": res["summary"],
                    "episode_ids": res["episode_ids"]})
    return out


ASSIST_APP = db.ASSIST_APP            # episodes that are prior Q&A, not activity

# Window titles are noisy: browsers append tab counts and profile names, and
# Windows appends "(Not Responding)". Stripping this junk roughly halves the
# prompt and stops the model fixating on "206 more pages".
_TITLE_NOISE = re.compile(
    r"\s*(and \d+ more pages?|\(Not Responding\)|- Personal|"
    r"- Microsoft.?\s*Edge|- Google Chrome|— Mozilla Firefox)", re.IGNORECASE)


def _clean_title(t):
    t = _TITLE_NOISE.sub("", t or "")
    return re.sub(r"\s*[-–—]\s*$", "", t).strip(" -–—·")


def _compress_events(events, cap=12, min_s=30):
    """Merge consecutive same-window events, drop flicker, keep the big ones.

    Amendment A calls for minimum required context: a raw 30-event dump is
    mostly duplicates of the same window. Returns [(app, title, dur_s, ts)].
    """
    merged = []
    for e in events:
        app, title = e["app"] or e["source"], _clean_title(e["title"])
        if merged and merged[-1][0] == app and merged[-1][1] == title:
            merged[-1][2] += e["dur_s"] or 0
        else:
            merged.append([app, title, e["dur_s"] or 0, e["ts"]])
    kept = [m for m in merged if m[2] >= min_s] or merged
    kept.sort(key=lambda m: m[2], reverse=True)
    kept = kept[:cap]
    kept.sort(key=lambda m: m[3])          # back to chronological
    return kept


def _context_lines(ctx):
    """Grouped, de-noised context. Returns (sections, eps, events).

    `sections` is a list of (heading, [lines]) so the prompt builder can frame
    each kind of evidence differently — observed activity is not the same
    thing as an answer PIOS gave earlier, and conflating them made the model
    cite its own past replies as if they were things the user did.
    """
    facts = [c for c in ctx if c["type"] == "fact"]
    eps = [c for c in ctx if c["type"] == "episode"]
    prior = [c for c in ctx if c["type"] == "assist"]
    events = [c for c in ctx if c["type"] == "event" and c["source"] != "chat"]
    res = next((c for c in ctx if c["type"] == "resume"), None)

    sections = []
    if res:
        # First, and labelled as state rather than history: "what should I pick
        # back up?" is unanswerable from episode summaries alone, and it's 1-3
        # lines, so it's cheaper to always include than to detect the intent.
        sections.append(("Where I left off (my current working state, right now)",
                         [res["summary"]]))
    if facts:
        sections.append(("Things PIOS has learned about me",
                         ["[fact %d] %s" % (f["id"], f["text"]) for f in facts]))
    if eps:
        # summaries already open with their own "HH:MM-HH:MM (N min):" — only
        # the date needs adding, or the model sees the time twice.
        sections.append(("Relevant past activity (from my computer usage log)",
                         ["[episode %d] %s %s" % (
                             e["id"], _day(e["start_ts"]),
                             _clean_title(e["summary"])) for e in eps]))
    comp = _compress_events(events)
    if comp:
        sections.append(("What I did today (merged, longest first shown in order)",
                         ["%s  %-16s %s (%s)" % (
                             _hm(ts), app, title or "(no title)", _dur(dur))
                          for app, title, dur, ts in comp]))
    if prior:
        sections.append(("Answers PIOS gave me earlier (reference only — NOT "
                         "things I did)",
                         ["[episode %d] %s" % (e["id"], e["summary"][:300])
                          for e in prior]))
    return sections, eps, events


def _dur(s):
    s = int(s or 0)
    return "%dm" % round(s / 60) if s >= 60 else "%ds" % s


def build_prompt(question, sections, web=False):
    """Assemble the outbound prompt.

    web=True produces a self-contained prompt for pasting into a fresh web AI
    chat (no system prompt available there), so it carries its own role
    framing and output spec.
    """
    body = "\n\n".join("## %s\n%s" % (h, "\n".join(ls)) for h, ls in sections)
    # Two kinds of question arrive here and they need opposite handling.
    # "What was I working on?" must be answered ONLY from the log. "How do I
    # do X in Google Sheets?" is general knowledge the log says nothing about
    # — an earlier version told the model to answer only from context either
    # way, so it refused perfectly answerable questions.
    rules = (
        "The context above is my personal activity log. It may or may not be "
        "relevant to my question.\n"
        "- If I'm asking about MY OWN activity, work, or history: answer only "
        "from the context and cite the entries you used by their [fact N] / "
        "[episode N] tags. If the context doesn't cover it, say so plainly "
        "rather than guessing.\n"
        "- If I'm asking a GENERAL question (how something works, how to do "
        "something, an explanation): just answer it properly from your own "
        "knowledge. Don't force the context in, and don't refuse because the "
        "log doesn't mention it. Use the context only if it genuinely adds "
        "something — e.g. it shows which tool or project I'm using.\n"
        "- Never state anything about my activity that isn't in the context.")
    if not web:
        return ("Context from the user's activity memory:\n%s\n\n"
                "Question: %s\n\n%s" % (body, question, rules))
    return (
        "You are helping me with a question. Below is context exported from "
        "PIOS, a local tool that records which application and window I had "
        "in focus and for how long. Personal details have been replaced with "
        "placeholders like [email-1] — treat them as opaque and do not ask "
        "about them.\n\n"
        "%s\n\n"
        "## My question\n%s\n\n"
        "## How to answer\n%s\n"
        "- Be concise and concrete. Lead with the answer, then any evidence.\n"
        "- Plain prose or short bullets. No preamble, no restating the question."
        % (body, question, rules))


def answer(con, question, cloud=False):
    """Retrieval-grounded answer. {'answer','sources','model'[,'redactions']}."""
    ctx = retrieve(con, question)
    sections, eps, events = _context_lines(ctx)
    sources = [e["id"] for e in eps]

    if cloud:
        out = _answer_cloud(con, question, sections, sources, eps)
    else:
        out = _answer_local(con, question, sections, sources, eps)

    db.insert_event(con, {"source": "chat", "app": "pios",
                          "title": question[:200],
                          "detail": out["answer"][:2000]})
    return out


def _answer_local(con, question, sections, sources, eps, prefix=""):
    result = None
    if sections and llm.available():
        result = llm.complete(
            build_prompt(question, sections),
            system=SYSTEM_PROMPT)
    if result:
        # No fallback to "all retrieved": if the model cited nothing then the
        # answer wasn't grounded in the log (a general question), and listing
        # every episode we happened to retrieve is a false provenance claim.
        cited = [i for i in sources
                 if re.search(r"episode\s+%d\b" % i, result)]
        return {"answer": prefix + result.strip(), "sources": cited,
                "model": llm.model_name()}
    if eps:
        body = "Here's what I found in memory:\n" + "\n".join(
            "- %s: %s (episode %d)" % (_day(e["start_ts"]), e["summary"], e["id"])
            for e in eps)
        return {"answer": prefix + body, "sources": sources,
                "model": "retrieval-only"}
    return {"answer": prefix + "No matching memories found for that question.",
            "sources": [], "model": "retrieval-only"}


# Manual Cloud Assistant (Amendment A, Priority 2): when no API can satisfy a
# cloud request, PIOS prepares a scrubbed, minimal prompt for the user to paste
# into a free web AI, then imports the reply back into memory with provenance.
ASSIST_PROVIDERS = [
    {"name": "Gemini", "url": "https://gemini.google.com/app"},
    {"name": "Claude", "url": "https://claude.ai/new"},
    {"name": "ChatGPT", "url": "https://chatgpt.com/"},
]


def _assist_payload(prompt, question, redactions):
    return {"prompt": prompt, "question": question, "redactions": redactions,
            "providers": ASSIST_PROVIDERS,
            "recommended": ASSIST_PROVIDERS[0]["name"]}


def import_assist(con, question, response, provider="web"):
    """Store a manually fetched web-AI answer as memory, with provenance."""
    ev_id = db.insert_event(con, {
        "source": "chat", "app": "pios", "title": question[:200],
        "detail": "[via %s web] %s" % (provider, response[:2000])})
    now = time.time()
    ep_id = db.insert_episode(con, {
        "start_ts": now, "end_ts": now,
        "summary": "Cloud-assisted answer (%s) — Q: %s — A: %s" % (
            provider, question[:120], response[:400]),
        "apps": ASSIST_APP, "source_event_ids": str(ev_id)})
    return ep_id


def _answer_cloud(con, question, sections, sources, eps):
    from . import cloud, config, gate  # lazy: keeps local path dep-free
    cfg = config.load()
    clean, redactions = gate.scrub(build_prompt(question, sections))

    ok, reason = gate.cloud_allowed(cfg)
    if ok:
        provs = ",".join(cloud.providers(cfg))
        db.log_egress(con, provs, clean, 1)  # EXACT bytes, logged before sending
        text, used, error = cloud.complete(
            clean, cfg=cfg,
            system=SYSTEM_PROMPT)
        if text:
            cited = [i for i in sources
                     if re.search(r"episode\s+%d\b" % i, text)]
            return {"answer": text.strip(), "sources": cited,
                    "model": "%s (cloud)" % used, "redactions": redactions}
        db.log_egress(con, used or provs, "cloud call failed: %s" % error, 0)
        prefix = "[cloud call failed (%s) — answered locally] " % error
    else:
        db.log_egress(con, "none", reason, 0)
        prefix = "[cloud unavailable: %s — answered locally] " % reason

    out = _answer_local(con, question, sections, sources, eps, prefix=prefix)
    if cfg.get("dev_mode", True):
        # dev mode never dead-ends: offer the manual web-assist flow. The web
        # prompt is self-contained (a fresh chat has no system prompt) and is
        # logged as egress the moment it's offered — it is exactly what the
        # user will paste.
        web_clean, web_redactions = gate.scrub(
            build_prompt(question, sections, web=True))
        db.log_egress(con, "manual-assist (prompt prepared for web paste)",
                      web_clean, 1)
        out["assist"] = _assist_payload(web_clean, question, web_redactions)
    return out


def build_brief(con):
    """Today (+yesterday) brief: time per app, notable episodes, last activity."""
    now = time.time()
    start = day_start(now)          # extends past midnight for late sessions
    events = db.recent_events(con, start - 86400)
    today = [e for e in events if e["ts"] >= start and e["source"] == "window"]

    app_min = Counter()
    for e in today:
        app_min[e["app"] or "?"] += (e["dur_s"] or 0) / 60
    eps = db.episodes_between(con, start - 86400, now)

    lines = ["Brief for %s" % _day(now)]
    if app_min:
        # say which window the totals cover, so a post-midnight brief that
        # includes yesterday evening isn't quietly mislabelled "today"
        label = ("today" if start == _midnight(now)
                 else "since %s" % _hm(start))
        lines.append("Time by app %s: " % label + ", ".join(
            "%s %d min" % (a, m) for a, m in app_min.most_common(5)))
    else:
        lines.append("No window activity recorded today.")
    if eps:
        lines.append("Recent episodes:")
        lines += ["- %s" % e["summary"] for e in eps[-5:]]
    acted = [e for e in events if e["source"] != "chat"]
    if acted:
        last = acted[-1]
        lines.append("Last activity: %s %s %s" % (
            _hm(last["ts"]), last["app"] or last["source"], last["title"] or ""))
    # heuristic only — the brief must render instantly and never load the model
    return "\n".join(lines)
