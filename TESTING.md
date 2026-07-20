# PIOS v0 — Testing

## What is already verified (ran during the build, all passing)

1. **Unit/integration suite** — `12 passed`:
   ```
   .venv\Scripts\python.exe -m pytest tests/ -q
   ```
   Covers: DB roundtrip + FTS5 search, punctuation-proof queries, episodization
   (gap splitting, provenance ids, fresh-event gating, idempotency), retrieval-only
   answer shape, every API route, FTS cleanup on delete. Tests use temp DBs and
   never touch your real data or require Ollama.

2. **Live end-to-end on real data** (verified in a browser during integration):
   - Sensor recorded real window activity within seconds of first launch.
   - Episodes built from real events; brief truthfully reconstructed the session.
   - Chat answered "what was I doing in my browser earlier tonight?" grounded in
     real episodes, with citations, `answered by gemma4`.
   - Privacy tab: toggles, watched folders, zero-egress explanation, data dir.
   - All API endpoints return 200.

3. **Bugs found and fixed during integration** (why integration testing exists):
   - gemma4 cold-load (~90s) blew the 60s LLM timeout → `keep_alive=30m` on every
     request + startup warmup thread + 180s timeout ([llm.py](pios/llm.py), [main.py](pios/main.py)).
   - SQLite thread-affinity error under FastAPI's threadpool → request-private
     connections with `check_same_thread=False` ([db.py](pios/db.py)).

## TestSprite (installed, one step left — needs YOUR api key)

TestSprite CLI 0.4.0 is installed globally and `testsprite doctor` passes every
check except credentials. I cannot create accounts on your behalf, so:

1. Get an API key from https://www.testsprite.com (dashboard → API key).
2. Run, in this folder, with PIOS running (`run_pios.bat`):
   ```
   set TESTSPRITE_API_KEY=sk-your-key
   testsprite setup --from-env --yes --agent claude
   testsprite doctor
   ```
3. Then create the first tests against the live app at `http://127.0.0.1:8321`:
   ```
   testsprite test create
   ```
   Suggested test descriptions to give it (one per test):
   - "Open http://127.0.0.1:8321 — the header shows a green core status dot and
     event/episode counts."
   - "In the Chat tab, ask 'what was I working on today?' — a non-empty answer
     appears with a sources line."
   - "The Today tab shows a Brief section and a Recent Activity list with
     timestamped entries."
   - "In the Memory tab, searching for a word from a visible episode returns that
     episode; deleting it removes it from the list."
   - "The Privacy tab shows sensor checkboxes and an egress log stating nothing
     has left the machine."
4. On failures: `testsprite test failure get` (self-contained failure bundle),
   fix, `testsprite test rerun`. Passing tests bank into the durable suite.

Note: TestSprite is a cloud service — its browser tests exercise your local app
but the test orchestration talks to api.testsprite.com. That is TestSprite's
traffic, not PIOS's; PIOS itself still makes zero cloud calls.
