# PIOS — Personal Intelligence Operating System

A privacy-first personal memory assistant that runs entirely on your own machine.

PIOS quietly records what you work on — which application and window had focus,
for how long, which files changed, which commits you made — turns that into a
searchable memory, and answers questions about your own work using a local AI
model. Nothing leaves your computer unless you explicitly ask it to.

**Status: v0.1.0, working prototype.** Windows only.

## What it does

- **Activity sensing** — active window titles, process names, watched folders,
  and git branch/commit activity. Signals only: never screenshots, never
  keystrokes, never window contents or file contents.
- **Episodic memory** — raw events are compressed into readable episodes, and
  past days are distilled into durable facts. Every derived memory links back
  to the events it came from, so nothing is asserted without provenance.
- **Ask your own history** — "what was I working on this morning?",
  "what should I pick back up?" — answered from your memory by a local model.
- **Daily brief and interrupt recovery** — where your time went, and how to
  resume what you were interrupted doing.
- **Privacy Gate** — the single choke point every outbound byte passes through:
  emails, credentials, file paths, and IPs are stripped, and the exact payload
  is written to a local egress ledger you can read.

## Privacy model

- All memory lives in one SQLite file in `%LOCALAPPDATA%\PIOS\`. It is never
  uploaded and never leaves the machine.
- The local server binds `127.0.0.1` only.
- Cloud AI is **off by default**. When enabled, requests are scrubbed and
  logged verbatim before sending, and routing prefers the local model first,
  then a free tier, then paid providers only with explicit opt-in.
- A manual assist mode prepares a sanitized prompt for you to paste into a web
  AI yourself, so the feature works with no API key at all.
- API keys are read from environment variables only — never stored by PIOS.

## Requirements

- Windows 10/11, Python 3.12+
- [Ollama](https://ollama.com) with a local model (default: `gemma3:4b`)

## Running it

```
run_pios.bat
```

Creates the virtual environment on first run, starts the local server, and
opens the UI at `http://127.0.0.1:8321`.

Optional: `set_api_keys.ps1` stores cloud API keys as user environment
variables (it prompts — keys are never written into this repo).

## Tests

```
.venv\Scripts\python.exe -m pytest tests/ -q     # 41 tests
.venv\Scripts\python.exe evals\run.py            # retrieval quality evals
```

The eval harness scores retrieval precision and recall against a labelled
fixture and fails below its threshold, so answer-quality regressions are
caught rather than discovered.

## Layout

```
pios/        core service — sensors, memory, privacy gate, cloud routing, API
ui/          single-file local web UI
tests/       test suite
evals/       golden retrieval evaluation harness
```

`SPEC-BUILD.md` documents the module contracts, `HANDOVER.md` covers daily use,
`TESTING.md` covers verification.
