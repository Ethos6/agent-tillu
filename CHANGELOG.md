# Changelog

## 0.2.1 — 2026-11-03

Review pass over every module. Fixed:

- A routine whose run was stopped while waiting for approval stayed marked "running" and never fired again.
- A message sent while a coworker was still replying was answered with a stale conversation (the earlier reply was missing); runs now load the conversation when they start, with the message they answer placed last.
- `web_search` results did not mark the run as tainted, so a standing approval could still apply after reading search results.
- Template guessing matched on common words ("site", "data") and could hand a new coworker the wrong personality and autonomy; it now only matches distinctive words.
- Watch mode blocked a watcher from writing its own notes, which it needs to compare runs; it may now write inside its workspace and still cannot act outside it.
- Webhook URLs showed `0.0.0.0` when bound to all interfaces.
- The web client now catches up on missed events after a dropped connection, and tolerates blocked browser storage.
- Python 3.11 compatibility (one f-string).

## 0.2.0 — 2026-11-03

- Renamed to **Agent Tillu**, with the Tillu logo.
- **Ollama native API**: when the model server is Ollama, chat goes through `/api/chat` with an explicit `num_ctx` (default 16384), so tool prompts are no longer silently truncated by Ollama's small default window.
- **Provenance on approval cards**: each card now says whether you named the destination yourself, the coworker chose it, or it appeared after reading outside content.
- **Re-verification at dispatch**: an approved action is inspected again right before it runs; if the page changed the button (or anything else material), it is held and re-asked.
- Deny is the primary button for money, destructive and outside-sourced actions.
- CI on Linux, macOS and Windows; CONTRIBUTING and SECURITY docs.

## 0.1.0 — 2026-11-02

- First version (as "Hearth"): Action Firewall, local memory with decay, routines, Foreman, browser, MCP, honesty guard.
