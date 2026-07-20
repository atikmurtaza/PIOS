"""Golden retrieval eval harness. E1-owned.

Fully deterministic: builds the synthetic fixture, runs memory.retrieve() for
every case in cases.json and scores it. No network, no Ollama — safe for CI.

    .venv\\Scripts\\python.exe evals/run.py      (exit 1 if below threshold)
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evals import fixture  # noqa: E402
from pios import memory  # noqa: E402

CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.json")

# Only these types are the activity context an answer is built from; 'assist'
# (prior Q&A) and unlabelled raw events are deliberately outside the scored set.
SCORED_TYPES = ("fact", "episode")


def _retrieved_labels(con, question, by_row):
    return {by_row[(c["type"], c["id"])]
            for c in memory.retrieve(con, question)
            if c["type"] in SCORED_TYPES and (c["type"], c["id"]) in by_row}


def score_case(case, got):
    """(score, recall, violations). Score = mean of recall and purity, so a
    case that retrieves everything it should but also drags in something it
    shouldn't cannot score 1.0."""
    must = set(case["must_retrieve"])
    never = set(case["must_not_retrieve"])
    recall = len(must & got) / len(must) if must else 1.0
    bad = never & got
    purity = 1.0 - len(bad) / len(never) if never else 1.0
    return (recall + purity) / 2, recall, bad


def run(verbose=True):
    spec = json.load(open(CASES, encoding="utf-8"))
    con, labels = fixture.build()
    by_row = {v: k for k, v in labels.items()}
    unknown = {l for c in spec["cases"]
               for l in c["must_retrieve"] + c["must_not_retrieve"]
               if l not in labels}
    assert not unknown, "cases.json references unknown labels: %s" % sorted(unknown)

    total, rows = 0.0, []
    for case in spec["cases"]:
        got = _retrieved_labels(con, case["question"], by_row)
        s, recall, bad = score_case(case, got)
        total += s
        rows.append((case["id"], s, recall, sorted(set(case["must_retrieve"]) - got),
                     sorted(bad)))
    con.close()
    overall = total / len(spec["cases"])

    if verbose:
        print("PIOS golden retrieval eval v%d — %d cases\n" % (
            spec["version"], len(spec["cases"])))
        print("%-32s %6s %6s  %s" % ("case", "score", "recall", "problems"))
        print("-" * 78)
        for cid, s, recall, missed, bad in rows:
            problems = ", ".join(
                ["MISSING " + m for m in missed] + ["LEAKED " + b for b in bad])
            print("%-32s %6.2f %6.2f  %s" % (cid, s, recall, problems or "ok"))
        print("-" * 78)
        print("overall %.3f (threshold %.2f) — %s" % (
            overall, spec["threshold"],
            "PASS" if overall >= spec["threshold"] else "FAIL"))
    return overall, spec["threshold"], rows


if __name__ == "__main__":
    ov, thr, _ = run()
    sys.exit(0 if ov >= thr else 1)
