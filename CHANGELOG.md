# Changelog

## 0.2.0 — 2026-11-03

- Renamed to **Agent Tillu**, with the Tillu logo.
- **Ollama native API**: when the model server is Ollama, chat goes through `/api/chat` with an explicit `num_ctx` (default 16384), so tool prompts are no longer silently truncated by Ollama's small default window.
- **Provenance on approval cards**: each card now says whether you named the destination yourself, the coworker chose it, or it appeared after reading outside content.
- **Re-verification at dispatch**: an approved action is inspected again right before it runs; if the page changed the button (or anything else material), it is held and re-asked.
- Deny is the primary button for money, destructive and outside-sourced actions.
- CI on Linux, macOS and Windows; CONTRIBUTING and SECURITY docs.

## 0.1.0 — 2026-11-02

- First version (as "Hearth"): Action Firewall, local memory with decay, routines, Foreman, browser, MCP, honesty guard.
