"""Deterministic synthetic memory for the golden retrieval evals. E1-owned.

Builds a realistic multi-day PIOS database with NO network and NO LLM: only
db.insert_* and memory.episodize(), which is pure heuristics. Episodes are
produced by the real episodizer from real events, so provenance is real.

Every episode/fact gets a stable `label`; the returned map is label ->
(kind, row_id), because row ids shift whenever the fixture changes.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pios import db, memory  # noqa: E402

DAY = 86400
H = 3600

# (label, days_ago, start_hour, [(app, title, dur_s), ...]) — events are laid
# end to end from start_hour. Blocks are >10 min apart so episodize() splits
# them exactly one episode per block.
BLOCKS = [
    ("ep-pios-schema", 3, 9.0, [
        ("Code.exe", "db.py - pios - Visual Studio Code", 1500),
        ("Code.exe", "schema notes.md - pios - Visual Studio Code", 900),
        ("Code.exe", "memory.py - pios - Visual Studio Code", 1200)]),
    ("ep-docs-fts5", 3, 11.5, [
        ("firefox.exe", "SQLite FTS5 Extension - sqlite.org", 1200),
        ("firefox.exe", "The bm25 ranking function - SQLite documentation", 900)]),
    # 12:30-14:10 gap: the meeting
    ("ep-email-invoices", 3, 14.5, [
        ("OUTLOOK.EXE", "Inbox - overdue invoice reminder from Acme Ltd", 600),
        ("OUTLOOK.EXE", "RE: invoicer rollout schedule - Outlook", 900)]),

    ("ep-invoicer-pdf", 2, 10.0, [
        ("Code.exe", "invoice_pdf.py - invoicer - Visual Studio Code", 1800),
        ("Code.exe", "templates/receipt.html - invoicer - Visual Studio Code", 900)]),
    ("ep-browse-stripe", 2, 13.0, [
        ("firefox.exe", "Stripe API reference - webhooks", 900),
        ("firefox.exe", "Testing Stripe webhooks locally with the CLI", 600)]),

    ("ep-pios-retrieval", 1, 9.5, [
        ("Code.exe", "retrieval notes - pios - Visual Studio Code", 1200),
        ("Code.exe", "test_pios.py - pios - Visual Studio Code", 1500)]),
    ("ep-invoicer-tax", 1, 15.0, [
        ("Code.exe", "tax_rates.py - invoicer - Visual Studio Code", 2100)]),

    ("ep-today-pios", 0, 9.0, [
        ("Code.exe", "memory.py - pios - Visual Studio Code", 1800),
        ("Code.exe", "evals/run.py - pios - Visual Studio Code", 1200)]),
]

# Prior Q&A, stored by import_assist() as manual-assist episodes. Deliberately
# worded like the retrieval-heavy topics so they compete with real activity.
ASSISTS = [
    ("assist-fts5-rank", "how do I rank sqlite fts5 results by relevance?",
     "Use the bm25() ranking function; lower is better. Order by bm25 and add "
     "a recency term if you want fresh rows first."),
    ("assist-bm25-weights", "what do bm25 column weights do in fts5?",
     "bm25 takes per-column weights so a match in the title column can score "
     "higher than a match in the body; sqlite fts5 defaults them all to 1.0."),
    ("assist-fts-index", "should I index sqlite fts5 external content tables?",
     "External content fts5 tables keep the index separate from the source "
     "table; you must keep them in sync with triggers."),
    ("assist-webhook-retry", "how should stripe webhook retries be handled?",
     "Stripe retries failed webhooks with exponential backoff; make the "
     "handler idempotent and verify the signature."),
    ("assist-fts-tokenizer", "which sqlite fts5 tokenizer should I use?",
     "The unicode61 tokenizer is the fts5 default and handles punctuation; "
     "porter adds stemming if you want ranking to match word variants."),
    ("assist-pdf-lib", "what is a good python pdf library for invoices?",
     "reportlab renders invoice PDFs from scratch; weasyprint converts an "
     "HTML receipt template instead."),
]

# (label, days_ago, text, [source episode labels]) — provenance is mandatory.
FACTS = [
    ("fact-vscode-projects", 2,
     "Almost all coding time is in Visual Studio Code, split between two "
     "projects: pios and invoicer.", ["ep-pios-schema", "ep-invoicer-pdf"]),
    ("fact-invoicer-billing", 2,
     "The invoicer project is about PDF invoice generation and tax rates for "
     "billing customers.", ["ep-invoicer-pdf"]),
    ("fact-meeting-slot", 3,
     "Wednesday early afternoon is usually away from the keyboard.",
     ["ep-email-invoices"]),
]


def anchor(now=None):
    """Fixed 17:00-local 'now' so a run is stable regardless of clock time."""
    return memory._midnight(now if now is not None else time.time()) + 17 * H


def build(path=":memory:", now=None):
    """Return (con, labels) with labels: label -> ('episode'|'fact', row_id)."""
    n = anchor(now)
    con = db.connect(path)
    starts = {}
    for label, days_ago, hour, items in BLOCKS:
        ts = memory._midnight(n) - days_ago * DAY + hour * H
        starts[ts] = label
        for app, title, dur in items:
            db.insert_event(con, {"source": "window", "app": app, "title": title,
                                  "ts": ts, "dur_s": dur})
            ts += dur

    made = memory.episodize(con, now=n)
    assert made == len(BLOCKS), "expected one episode per block, got %d" % made
    labels = {}
    for e in db.episodes_between(con, 0, n):
        labels[starts[e["start_ts"]]] = ("episode", e["id"])
    assert len(labels) == len(BLOCKS), "block start_ts did not map 1:1"

    for label, days_ago, text, srcs in FACTS:
        day = memory._ymd(n - days_ago * DAY)
        fid = db.insert_fact(con, {
            "ts": n - days_ago * DAY, "day": day, "text": text,
            "source_episode_ids": ",".join(
                str(labels[s][1]) for s in srcs)})
        labels[label] = ("fact", fid)

    for label, q, a in ASSISTS:
        labels[label] = ("episode", memory.import_assist(con, q, a, "Gemini"))

    # Today's tail: inserted after episodize() so it stays raw — this is what
    # feeds "what I did today" and resume().
    ts = memory._midnight(n) + 16 * H
    for app, title, dur in [
            ("Code.exe", "invoice_pdf.py - invoicer - Visual Studio Code", 1200),
            ("firefox.exe", "Stripe webhook signature verification", 600)]:
        db.insert_event(con, {"source": "window", "app": app, "title": title,
                              "ts": ts, "dur_s": dur})
        ts += dur
    return con, labels


if __name__ == "__main__":
    c, lab = build()
    print("%d labels, %d events" % (len(lab), db.stats(c)["events"]))
    for k in sorted(lab):
        print("  %-22s %s %s" % (k, lab[k][0], lab[k][1]))
