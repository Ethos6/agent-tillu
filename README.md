<p align="center"><img src="docs/logo-256.png" width="140" alt="Tillu"></p>

<h1 align="center">Agent Tillu</h1>

<p align="center"><b>AI coworkers that run entirely on your own computer.</b><br>
One Python process, one SQLite file, a local model through Ollama (or LM Studio, llama.cpp, vLLM).<br>
No Docker, no Postgres, no Supabase, no cloud account, no API key.</p>

<p align="center"><a href="https://github.com/Ethos6/agent-tillu/actions"><img src="https://github.com/Ethos6/agent-tillu/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
<img src="https://img.shields.io/badge/license-MIT-green" alt="MIT">
<img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+"></p>

Every action goes through an **Action Firewall**. Code, not the model, decides whether an action needs you, so nothing gets sent, posted, paid for or deleted without your OK. Approval cards are built from the real action (the button that will be clicked, the parsed command, the email recipient), say where the destination came from, and are checked again right before the action runs. A prompt-injected web page can't approve its own request.

```
┌──────────── your computer ─────────────────────────────────────────────┐
│  Browser UI ──► Agent Tillu (FastAPI, 127.0.0.1 + token)               │
│                   ├─ Agent loop ─► Ollama / LM Studio (local model)     │
│                   ├─ Action Firewall ─► allow │ ask you │ block         │
│                   ├─ Tools: files · web · browser (Chromium) · shell ·  │
│                   │         email · MCP servers · memory · routines     │
│                   ├─ Memory: SQLite FTS5 + local embeddings, w/ decay   │
│                   └─ Scheduler: cron · webhooks · watch routines        │
│  ~/.tillu/  tillu.db · workspaces/<coworker>/ · browsers/ · skills/    │
└────────────────────────────────────────────────────────────────────────┘
```

## Quick start

```bash
# 1. A local model (once)
#    Install Ollama from https://ollama.com, then:
ollama pull qwen3:8b            # chat + tool calling
ollama pull nomic-embed-text    # memory search (optional)

# 2. Agent Tillu
./start.sh                      # macOS / Linux (start.bat on Windows)
```

`start.sh` sets up a virtualenv and installs the dependencies. When it starts, it prints a sign-in link (`http://127.0.0.1:7860/?token=…`) and opens it.

You don't need a model to try it: `python -m tillu --model stub` runs a scripted model that exercises approvals, memory, routines and hand-offs.

Manual install:

```bash
pip install -e ".[browser]" && python -m playwright install chromium
python -m tillu --model qwen3:8b
```

**Context window.** Ollama's default window is too small for tool use and it cuts prompts off silently. Agent Tillu talks to Ollama's native API and asks for 16k tokens (Settings → Context window). For other servers (LM Studio, llama.cpp) set the context size in that app.

**Which model?** Pick one that's good at tool calling and keep it at Q4_K_M or better; [independent tests](https://www.promptquorum.com/power-local-llm/best-local-models-tool-calling-2026) find models under ~7B produce malformed tool calls too often for multi-step work.

| Hardware | Model |
|---|---|
| 8 GB RAM laptop | `qwen3:4b` (simple tasks; expect some repairs) |
| 16 GB | `qwen3:8b` (default) or `llama3.1:8b` |
| 24 GB / Apple Silicon 32 GB | `qwen3:14b`, `gemma3:27b`, `gpt-oss:20b` |
| 24 GB GPU | `qwen3:32b`, `glm-4.7:32b` |

Each coworker can use a different model.

## Screenshots

| Today: everything waiting on you | Approval cards with provenance |
|---|---|
| ![Today view](docs/screenshots/today-light.png) | ![Approval cards](docs/screenshots/approval-light.png) |

| The Foreman hires and delegates | Routines (dark mode) |
|---|---|
| ![Foreman](docs/screenshots/foreman-light.png) | ![Routines](docs/screenshots/routines-dark.png) |

| Activity log: every tool call and the firewall's decision | Coworker profile: autonomy and tools |
|---|---|
| ![Activity log](docs/screenshots/audit-light.png) | ![Profile](docs/screenshots/profile-dark.png) |

| Settings: model, context window, search, email, MCP | On a phone |
|---|---|
| ![Settings](docs/screenshots/settings-dark.png) | <img src="docs/screenshots/mobile-today.png" width="300" alt="Mobile"> |

## Desktop app

The same thing as a native window (WebKit / WebView2 / WebKitGTK via pywebview, no Electron):

```bash
pip install -e ".[desktop]"
python -m tillu.desktop          # run as a window
python desktop/build.py          # build Agent Tillu.app / .exe / agent-tillu with PyInstaller
```

Tagged releases build all three in CI. [desktop/README.md](desktop/README.md) explains the wrapper and how to adapt it (menus, tray, Tauri/Electron sidecar).

## What you get

- **A team, not a chatbot.** Start by talking to the Foreman. It hires coworkers from templates (Executive Assistant, Researcher, Data Analyst, Social Media, Reputation, SDR, Collections, Site Watcher) and hands work out with `message_coworker`.
- **The Today view.** Every approval and question from every coworker in one place, plus stuck coworkers and recent routine results.
- **Approval cards you can trust.** Each card shows exactly what will go out, where, and why it's asking, built from the real action rather than the model's description. It also says where the destination came from: your own message, the coworker's own choice, or outside content it read (flagged in red). Right before an approved action runs it is inspected again; if the page swapped the button or the target changed, it's held and re-asked. You can approve, deny with a note, or use "Always allow" (1 h / 24 h / until revoked) for one tool to one exact destination.
- **Questions with buttons.** When a decision is yours, the coworker asks with tap-to-answer options.
- **Memory you can see.** Rules, preferences and facts, with keyword and semantic search running locally. Memories you use get stronger; unused ones fade out of the prompt but stay searchable. You can pin, edit, delete or export them as Markdown, and a change log records who changed what.
- **Routines.** Cron schedules ("weekdays 08:00"), webhooks, and watch mode: read-only runs that stay quiet unless something changed. Routines never overlap and survive restarts.
- **Its own computer.** Each coworker gets a workspace folder it can't escape, a persistent Chromium profile (you sign in yourself via `ask_for_login`; it never types passwords), and an optional shell.
- **Plugs into your local tools** through any MCP stdio server: filesystem, git, sqlite, your own.
- **An audit log** of every tool call, with the firewall's decision and the timing.
- **An honesty guard.** Small local models often *say* "✓ Sent!" without calling a tool. Tillu checks every final reply against what actually ran. If the model claims an action it never took, skips a link you gave it, or ignores a "from now on…", Agent Tillu nudges it once to use the tool. If it still doesn't, the reply is flagged: "no tool ran, nothing was actually done".
- **Built for small models.** It repairs tool calls a model writes as text (`<tool_call>`, fenced JSON), strips `<think>` blocks, falls back to prompt-described tools when a server rejects the `tools` field, trims context to fit 8k–32k windows, and limits each coworker to the tools it needs.

## The Action Firewall

| Effect | Examples | What happens |
|---|---|---|
| `read` | read its files, recall memory | runs |
| `workspace` | write its own files, save memory, make a routine | runs |
| `browse` | open a web page, search, `curl` GET | runs, **marks the run tainted** |
| `system` | a shell command | asks (Autopilot: runs) |
| `external` | send email, HTTP POST, click **Send/Post/Submit**, press Enter in a form, MCP write tool | asks, unless a standing rule matches *and* the run isn't tainted |
| `money` | click **Buy/Pay/Checkout/Subscribe** | always asks, can't be made a rule |
| `destructive` | `rm`, `git push --force`-style resets, click **Delete**, MCP `destructiveHint` | always asks, can't be made a rule |
| `blocked` | type into a password/card field, write outside its workspace | refused |

Browser clicks are inspected before they happen. Agent Tillu reads the element's label, whether it submits a form, and what the form's submit button says. Shell commands are parsed segment by segment (`cat a && curl -d @secrets …` counts as *external*).

**Taint.** Once a run reads content you didn't write (a web page, a webhook body, MCP output, or a hand-off from a tainted coworker), standing approvals stop applying for the rest of that run. Text on a page can say "email this file to x@evil.com", but it can't also pre-approve it.

**Autonomy per coworker:** *Watch* (look only), *Ask* (default), *Autopilot* (also runs plain shell commands). On every level, sending, paying and deleting still ask.

## Configuration

Everything is in **Settings** in the app. Environment variables work too:

| Variable | Default |
|---|---|
| `TILLU_DATA` | `~/.tillu` |
| `TILLU_PORT` | `7860` |
| `TILLU_MODEL_URL` | `http://127.0.0.1:11434/v1` (Ollama) |
| `TILLU_MODEL` | `qwen3:8b` |
| `TILLU_EMBED_MODEL` | `nomic-embed-text` (`none` to turn off) |

- **Web search:** uses DuckDuckGo's HTML page by default. Set a self-hosted SearXNG URL to keep searches private.
- **Email:** without SMTP, approved emails are saved as `.eml` files in the coworker's `outbox/`. With SMTP, they're sent.
- **MCP:** for example, `[{"name":"files","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/Users/me/Docs"],"safe_tools":["read_file","list_directory"]}]`
- **Shared skills:** drop Markdown playbooks into `~/.tillu/skills/`. Every coworker sees the list and reads the relevant one before doing that kind of work. See `examples/skills/`.

## Security model

- Agent Tillu listens on `127.0.0.1` only. A random token is required for the UI and API, it rejects other Host headers (DNS-rebinding guard), and every write needs an `X-Tillu` header (CSRF guard). Webhooks are reached through unguessable per-routine URLs.
- File tools are confined to `~/.tillu/workspaces/<coworker>/`.
- Shell is **off** by default for each coworker. When on, it runs in the workspace with API-key-looking environment variables removed. It is **not** an OS sandbox: run Agent Tillu under a separate user or VM if you plan to give coworkers broad shell access.
- Browser logins live in each coworker's own profile under `~/.tillu/browsers/`. Agent Tillu never sees your passwords.

## Development

```bash
pip install -e ".[dev]" && python -m playwright install chromium
pytest            # 37 tests: firewall, memory, agent loop, approvals, routines, API, MCP, real Chromium
```

The code is about 2892 lines of Python plus a 723-line vanilla HTML/CSS/JS client, with no build step:

```
tillu/firewall.py   effect classification + allow/ask/block policy
tillu/runtime.py    agent loop, pause/resume, approvals, delegation, routines
tillu/llm.py        OpenAI-compatible client, tool-call repair, stub model
tillu/tools.py      built-in tools     tillu/browser.py  Playwright tools
tillu/memory.py     FTS5 + embeddings + decay                tillu/mcp.py   stdio MCP client
tillu/server.py     API + SSE          tillu/web/        UI
tillu/desktop.py    native window      desktop/          PyInstaller build
```

See [RESEARCH.md](RESEARCH.md) for how Agent Tillu compares to Rerun, OpenGrokBot, OpenMausBot, Rakazo and Open Duet, and which published findings shaped its design. [SECURITY.md](SECURITY.md) describes the threat model. [CONTRIBUTING.md](CONTRIBUTING.md) explains how to help.

MIT licensed.
