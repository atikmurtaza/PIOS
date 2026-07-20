# PIOS v0.1.0 — Handover

**Status: built, tested, and running on this machine, already recording your real
activity.** This is the MVP's walking skeleton,
built by the three engineer subagents defined in `.claude/agents/`.

## What you have

A fully local personal-memory assistant:
- **Window sensor** — records which app/window has focus and for how long
  (titles + process names only; never screenshots, keystrokes, or contents).
- **File sensor** — optional; watches folders you list in Privacy tab.
- **Episodic memory** — every 5 minutes, raw events older than 15 min are
  compressed into truthful episode summaries (gemma4 polishes the wording; every
  episode keeps links to its source events).
- **Chat** — ask about your own activity; answers are grounded in your memory
  with episode citations, answered by local gemma4.
- **Daily brief** — Today tab: time per app, episodes, last activity.
- **Privacy by architecture** — everything lives in
  `C:\Users\atikm\AppData\Local\PIOS\pios.db`. The server binds 127.0.0.1 only.
  The ONLY network call in the codebase is to your local Ollama
  (127.0.0.1:11434). The egress log proves it. Zero cloud calls.

## Daily use

| To | Do |
|---|---|
| Start PIOS | double-click `run_pios.bat` (starts server + opens the UI) |
| Open the UI | http://127.0.0.1:8321 |
| Start at login (opt-in) | run `install_autostart.bat` once |
| Stop | Ctrl+C in the PIOS window (or close it) |
| Ask your memory | Chat tab — best after a few hours of accrued activity |
| Morning check-in | Today tab → Brief |
| See/delete what it knows | Memory tab (delete cascades to search index) |
| Pause observation | Privacy tab → untick sensors → Save → restart PIOS |
| Watch code folders | Privacy tab → add folder paths (one per line) → restart |
| Delete everything | stop PIOS, delete `%LOCALAPPDATA%\PIOS\` |
| Back up your memory | copy `%LOCALAPPDATA%\PIOS\pios.db` (one file) |

## Verifying it with your real data (the point of this build)

Let it run through a normal day, then judge it on the five MVP promises:
1. "What was I doing at 11am?" → Chat should answer truthfully with citations.
2. Morning: does the Brief honestly describe yesterday/today?
3. After an interruption: ask "what was I in the middle of?"
4. Memory tab: is anything recorded that you'd rather not have? Delete it —
   and consider unticking a sensor or using idle-pause behavior.
5. Egress log: still empty. Verify externally anytime with GlassWire/Wireshark —
   pios.exe/python should show zero non-localhost traffic.

## Testing

`TESTING.md` — 12-test suite passes; TestSprite CLI installed, needs your API
key (one command, instructions inside).

## Known limits (deliberate)

- First chat after a cold start pauses while the local model loads; later
  answers are much faster (the model unloads again after ~5 minutes idle).
- Sensor config changes need a PIOS restart.
- Cloud routing (Amendment A): local model first; then free Gemini tier
  (`GEMINI_API_KEY`); paid Claude/OpenAI only if "Allow paid APIs" is on in
  the Privacy tab; and a Manual Web Assist fallback (clipboard + paste-back,
  no key needed) whenever no API can answer. Every outbound or prepared
  prompt is scrubbed and logged verbatim in the egress ledger.
- Browser tab titles come from the window title only (no extension yet), so all
  tabs read as "... - Microsoft Edge".

## Development

Engineer subagent definitions live in `.claude/agents/` (pios-e1-ai,
pios-e2-desktop, pios-e3-fullstack). `SPEC-BUILD.md` is the module contract;
`TESTING.md` covers the test suite and the golden retrieval evals.
