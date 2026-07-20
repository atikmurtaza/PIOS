"""Golden retrieval eval, run under pytest so a plain `pytest` catches
retrieval regressions. Deterministic: in-memory fixture, no Ollama."""
import pytest

from evals import run
from pios import memory


def test_golden_retrieval_meets_threshold():
    overall, threshold, rows = run.run(verbose=False)
    problems = [(cid, missed, bad) for cid, s, _, missed, bad in rows if s < 1.0]
    assert overall >= threshold, "score %.3f < %.2f; %s" % (
        overall, threshold, problems)


@pytest.mark.parametrize("kind", ["resume", "assist"])
def test_context_sections_frame_state_and_prior_qa(kind):
    """resume() reaches the prompt; prior Q&A is present but kept out of the
    activity section (the two fixes the eval scores indirectly)."""
    from evals import fixture
    con, labels = fixture.build()
    ctx = memory.retrieve(con, "sqlite fts5 bm25 ranking")
    sections, eps, _ = memory._context_lines(ctx)
    headings = [h for h, _ in sections]

    if kind == "resume":
        assert headings[0].startswith("Where I left off")
        assert "invoice_pdf.py" in sections[0][1][0]   # today's last real window
    else:
        assert any(h.startswith("Answers PIOS gave me earlier") for h in headings)
        assert all(e["apps"] != memory.ASSIST_APP for e in eps)
        assert labels["assist-fts5-rank"][1] not in [e["id"] for e in eps]
    con.close()


def test_resume_section_absent_without_activity():
    from pios import db
    con = db.connect(":memory:")
    sections, eps, events = memory._context_lines(memory.retrieve(con, "anything"))
    assert sections == [] and eps == [] and events == []
    con.close()
