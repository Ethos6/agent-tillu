"""The runtime: agent loop, approvals, questions, delegation, hiring and routines."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import secrets
import time
import traceback
import uuid
from typing import Any

from croniter import croniter

from .browser import BrowserManager, browser_tools
from .config import Config
from .db import DB, now
from .events import EventBus
from .firewall import EFFECT_ORDER, Classification, decide
from .llm import LLM
from .mcp import MCPManager
from .memory import Memory
from .templates import TEMPLATES, system_prompt
from .tools import DEFAULT_GROUPS, PauseForUser, Tool, ToolCtx, builtin_tools

MAX_HOPS = 3
QUIET = re.compile(r"^\s*(NOTHING_NEW|nothing new\.?|no changes?\.?)\s*$", re.I)
CLAIM = re.compile(r"(✓|✅|\b(sent|emailed|e-mailed|saved|remembered|noted down|scheduled|created|posted|published|deleted|"
                   r"wrote|written|booked|ordered|paid|uploaded|submitted|downloaded|read (?:the|it|your)|summari[sz]ed|fetched|"
                   r"(?:i|i've|i have) (?:added|updated|set up|stored|checked|looked))\b)", re.I)
LASTING = re.compile(r"^\s*(please\s+)?remember\b|\bfrom now on\b|\b(always|never) (?:use|send|email|write|call|reply|cc|include)\b|\bi (?:prefer|like|hate|don't like)\b|\bmy \w+ (?:is|are)\b", re.I)
URL_RE = re.compile(r"https?://\S+")
WATCH_TOOLS = {"recall", "remember", "list_files", "read_file", "write_file", "web_fetch", "web_search", "browser_open", "browser_read", "browser_scroll"}


class Paused(Exception):
    pass


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "coworker"
    return f"{s[:24]}-{uuid.uuid4().hex[:4]}"


def describe_cron(expr: str) -> str:
    try:
        m, h, dom, mon, dow = expr.split()
    except ValueError:
        return expr
    days = {"*": "every day", "1-5": "weekdays", "0,6": "weekends", "6,0": "weekends", "0": "Sundays", "1": "Mondays",
            "2": "Tuesdays", "3": "Wednesdays", "4": "Thursdays", "5": "Fridays", "6": "Saturdays"}
    if m.startswith("*/") and h == "*":
        return f"every {m[2:]} minutes"
    if h.startswith("*/") and m.isdigit():
        return f"every {h[2:]} hours"
    if m.isdigit() and h == "*":
        return f"every hour at :{int(m):02d}"
    if m.isdigit() and h.isdigit() and dom == "*" and mon == "*":
        return f"{days.get(dow, 'on days ' + dow)} at {int(h):02d}:{int(m):02d}"
    if m.isdigit() and h.isdigit() and dom.isdigit() and mon == "*":
        return f"monthly on day {dom} at {int(h):02d}:{int(m):02d}"
    return expr


TEMPLATE_HINTS = {
    "researcher": ["researcher", "research", "analyst-research"],
    "data-analyst": ["analyst", "analytics", "data", "kpi", "reporting"],
    "social-media": ["social", "marketing", "content", "posts"],
    "reputation": ["reputation", "reviews", "review"],
    "sdr": ["sdr", "sales", "prospect", "prospects", "leads", "outreach"],
    "collections": ["collections", "invoice", "invoices", "receivables"],
    "site-watcher": ["watcher", "monitor", "monitoring", "watch"],
    "executive-assistant": ["assistant", "executive", "inbox", "calendar", "secretary"],
}


def guess_template(role: str) -> str:
    """Pick a template from a free-text job description, only on distinctive words."""
    words = set(re.findall(r"[a-z]+", role.lower()))
    for key, hints in TEMPLATE_HINTS.items():
        if words & set(hints):
            return key
    return ""


class Runtime:
    def __init__(self, cfg: Config, llm: LLM | None = None):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.bus = EventBus()
        self.llm = llm or LLM(self._setting("model_base_url", cfg.model_base_url), self._setting("model_api_key", cfg.model_api_key))
        self.memory = Memory(self.db, self.llm, self._setting("embed_model", cfg.embed_model))
        self.browser = BrowserManager(cfg.data_dir / "browsers", headless=self._setting("browser_headless", True))
        self.mcp = MCPManager()
        self.locks: dict[str, asyncio.Lock] = {}
        self.stop_flags: set[int] = set()
        self.tasks: set[asyncio.Task] = set()
        self._scheduler: asyncio.Task | None = None
        self._recover()
        if not self.db.one("SELECT id FROM agents LIMIT 1"):
            self.hire(name="Foreman", role=TEMPLATES["foreman"]["role"], template="foreman", is_foreman=True)

    # --- settings ---------------------------------------------------------------------------------
    def _setting(self, key: str, default: Any) -> Any:
        try:
            return self.db.get_setting(key, default)
        except AttributeError:  # db not ready
            return default

    def model_for(self, agent: dict) -> str:
        return agent.get("model") or self.db.get_setting("default_model", self.cfg.default_model)

    def apply_settings(self) -> None:
        self.llm.base_url = self.db.get_setting("model_base_url", self.cfg.model_base_url).rstrip("/")
        self.llm.api_key = self.db.get_setting("model_api_key", self.cfg.model_api_key)
        self.llm.num_ctx = int(self.db.get_setting("num_ctx", self.cfg.num_ctx) or 16384)
        self.llm.reset()
        self.memory.embed_model = self.db.get_setting("embed_model", self.cfg.embed_model)
        self.browser.headless = self.db.get_setting("browser_headless", True)

    # --- lifecycle --------------------------------------------------------------------------------
    def _recover(self) -> None:
        """After a crash/restart: runs that were mid-flight can't continue; routines aren't stuck running."""
        for r in self.db.all("SELECT id, agent_id FROM runs WHERE status IN ('running','queued')"):
            self.db.update("runs", "id", r["id"], {"status": "error", "updated_at": now()})
            self.post(r["agent_id"], "system", "⚠️ Agent Tillu restarted while I was working; that task was interrupted. Ask again to retry.", run_id=r["id"])
        self.db.execute("UPDATE routines SET running=0")

    async def start(self) -> None:
        self.bus.loop = asyncio.get_running_loop()
        await self.mcp.load(self.db.get_setting("mcp_servers", []))
        self._scheduler = asyncio.create_task(self._schedule_loop())

    async def shutdown(self) -> None:
        if self._scheduler:
            self._scheduler.cancel()
        for t in list(self.tasks):
            t.cancel()
        await self.mcp.stop()
        await self.browser.close()

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    # --- messages & status ------------------------------------------------------------------------
    def post(self, agent_id: str, role: str, content: str, meta: dict | None = None, run_id: int | None = None) -> dict:
        mid = self.db.insert("messages", {"agent_id": agent_id, "run_id": run_id, "role": role, "content": content,
                                          "meta": json.dumps(meta or {}), "created_at": now()})
        row = self.db.one("SELECT * FROM messages WHERE id=?", (mid,))
        row["meta"] = json.loads(row["meta"])
        self.bus.emit("message", row)
        return row

    def agent_state(self, agent_id: str) -> str:
        if self.db.one("SELECT 1 FROM runs WHERE agent_id=? AND status='running'", (agent_id,)):
            return "working"
        if self.db.one("SELECT 1 FROM approvals WHERE agent_id=? AND status='pending'", (agent_id,)) or \
           self.db.one("SELECT 1 FROM questions WHERE agent_id=? AND status='pending'", (agent_id,)):
            return "waiting"
        last = self.db.one("SELECT status FROM runs WHERE agent_id=? ORDER BY id DESC LIMIT 1", (agent_id,))
        return "stuck" if last and last["status"] in ("error", "budget") else "idle"

    def emit_status(self, agent_id: str) -> None:
        self.bus.emit("status", {"agent_id": agent_id, "state": self.agent_state(agent_id)})

    # --- agents -----------------------------------------------------------------------------------
    def get_agent(self, agent_id: str) -> dict | None:
        return self.db.one("SELECT * FROM agents WHERE id=?", (agent_id,))

    def find_agent(self, name: str) -> dict | None:
        name = name.strip().lstrip("@").lower()
        return self.db.one("SELECT * FROM agents WHERE lower(name)=? OR id=?", (name, name))

    def hire(self, *, name: str, role: str, template: str = "", instructions: str = "", tools: Any = None,
             is_foreman: bool = False, model: str = "", autonomy: str | None = None) -> dict:
        if not template and not is_foreman:
            template = guess_template(role)
        t = TEMPLATES.get(template, {})
        name = (name or t.get("name") or "Coworker").strip()[:40]
        base, n = name, 2
        while self.find_agent(name):
            name, n = f"{base} {n}", n + 1
        groups = [g for g in (tools if isinstance(tools, list) and tools else t.get("groups", DEFAULT_GROUPS))]
        if "core" not in groups:
            groups.insert(0, "core")
        if not is_foreman:
            groups = [g for g in groups if g != "foreman"]
        soul = "\n\n".join(x for x in (t.get("soul", ""), instructions) if x)
        aid = "foreman" if is_foreman and not self.get_agent("foreman") else slug(name)
        self.db.insert("agents", {
            "id": aid, "name": name, "role": (role or t.get("role") or "General help")[:200], "soul": soul, "model": model,
            "tools": json.dumps(groups), "autonomy": autonomy or t.get("autonomy", "ask"), "shell_enabled": int(bool(t.get("shell"))),
            "emoji": t.get("emoji", "🙂"), "is_foreman": int(is_foreman), "created_at": now(),
        })
        agent = self.get_agent(aid)
        self.bus.emit("agents", {"id": aid})
        self.post(aid, "system", f"{agent['emoji']} {name} joined the team — {agent['role']}.")
        return agent

    def tools_for(self, agent: dict, watch: bool = False) -> dict[str, Tool]:
        all_tools = builtin_tools()
        if self.browser.available:
            all_tools.update(browser_tools(self.browser))
        all_tools.update(self.mcp.tools())
        groups = set(json.loads(agent["tools"] or "[]")) | {"core"}
        if agent.get("is_foreman"):
            groups.add("foreman")
        chosen = {n: t for n, t in all_tools.items() if t.group in groups}
        if watch:
            chosen = {n: t for n, t in chosen.items() if n in WATCH_TOOLS}
        return chosen

    # --- runs -------------------------------------------------------------------------------------
    def lock(self, agent_id: str) -> asyncio.Lock:
        return self.locks.setdefault(agent_id, asyncio.Lock())

    def _history(self, agent_id: str, limit: int = 30) -> list[dict]:
        rows = self.db.all("SELECT role, content, meta FROM messages WHERE agent_id=? AND role IN ('user','agent','relay','relay_result','answer') "
                           "ORDER BY id DESC LIMIT ?", (agent_id, limit))
        out = []
        for r in reversed(rows):
            if r["role"] == "agent":
                out.append({"role": "assistant", "content": r["content"]})
            elif r["role"] == "relay":
                meta = json.loads(r["meta"])
                out.append({"role": "user", "content": f"[Task from coworker {meta.get('from_name','?')}]\n{r['content']}"})
            elif r["role"] == "relay_result":
                meta = json.loads(r["meta"])
                out.append({"role": "user", "content": f"[Reply from coworker {meta.get('from_name','?')}]\n{r['content']}"})
            else:
                out.append({"role": "user", "content": r["content"]})
        return out

    def _trim(self, messages: list[dict]) -> list[dict]:
        budget = self.cfg.context_chars
        size = lambda m: len(m.get("content") or "") + len(json.dumps(m.get("tool_calls", "")))  # noqa: E731
        total = sum(size(m) for m in messages)
        if total <= budget:
            return messages
        msgs = [dict(m) for m in messages]
        # 1) shrink old tool outputs, oldest first, keep the last 4 intact
        tool_idx = [i for i, m in enumerate(msgs) if m["role"] == "tool"][:-4]
        for i in tool_idx:
            if total <= budget:
                break
            before = size(msgs[i])
            msgs[i]["content"] = (msgs[i]["content"] or "")[:300] + "\n…[older tool output trimmed to save context]"
            total -= before - size(msgs[i])
        # 2) drop the oldest chat turns (never split an assistant tool_call from its results)
        while total > budget and len(msgs) > 6:
            m = msgs.pop(0)
            total -= size(m)
            while msgs and msgs[0]["role"] == "tool":
                total -= size(msgs.pop(0))
        return msgs

    def _system(self, agent: dict, run: dict, tool_names: list[str]) -> str:
        skills = []
        for f in sorted(self.cfg.shared_skills.glob("*.md"))[:30]:
            first = next((ln.strip("# ").strip() for ln in f.read_text(errors="ignore").splitlines() if ln.strip()), "")
            skills.append({"name": f.stem, "description": first[:120]})
        coworkers = self.db.all("SELECT name, role FROM agents WHERE id != ?", (agent["id"],))
        routines = self.db.all("SELECT id, name, cron, mode FROM routines WHERE agent_id=? AND enabled=1", (agent["id"],))
        return system_prompt(agent, memories=self.memory.core(agent["id"]), skills=skills if "read_skill" in tool_names else [],
                             coworkers=coworkers, routines=routines, tool_names=tool_names, origin=run["origin"])

    async def send(self, agent_id: str, text: str) -> dict:
        """The person sent a message."""
        msg = self.post(agent_id, "user", text)
        run = self._new_run(agent_id, "operator", text, history=True)
        self.spawn(self._drive(run["id"]))
        return msg

    def _new_run(self, agent_id: str, origin: str, text: str, *, history: bool, hop: int = 0, parent_run_id: int | None = None,
                 routine_id: int | None = None, tainted: bool = False, taint_reason: str = "", watch: bool = False) -> dict:
        # With history, the conversation is loaded when the run actually starts (see _loop), so a run queued behind
        # another one still sees that run's reply.
        state = {"messages": [] if history else [{"role": "user", "content": text}], "seed": text if history else None, "watch": watch}
        rid = self.db.insert("runs", {"agent_id": agent_id, "origin": origin, "status": "queued", "state": json.dumps(state),
                                      "tainted": int(tainted), "taint_reason": taint_reason, "hop": hop, "parent_run_id": parent_run_id,
                                      "routine_id": routine_id, "created_at": now(), "updated_at": now()})
        return self.db.one("SELECT * FROM runs WHERE id=?", (rid,))

    def _save(self, run: dict, state: dict, **changes) -> None:
        self.db.update("runs", "id", run["id"], {"state": json.dumps(state), "updated_at": now(), **changes})
        run.update(changes)

    async def _drive(self, run_id: int, resume: list[dict] | None = None) -> None:
        """Run (or resume) a run until it finishes or pauses for the person."""
        run = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        agent = self.get_agent(run["agent_id"])
        if not agent:
            return
        async with self.lock(agent["id"]):
            self.db.update("runs", "id", run_id, {"status": "running", "updated_at": now()})
            run["status"] = "running"
            self.emit_status(agent["id"])
            state = json.loads(run["state"])
            try:
                await self._loop(agent, run, state, resume)
            except Paused:
                pass
            except asyncio.CancelledError:
                self._save(run, state, status="stopped")
                raise
            except Exception as e:  # surface the error in the thread; the run is marked stuck
                traceback.print_exc()
                self._save(run, state, status="error")
                self.post(agent["id"], "system", f"⚠️ {agent['name']} hit an error: {e}", run_id=run_id)
                await self._finish(agent, run, None)
            finally:
                self.stop_flags.discard(run_id)
                self._release_routine(run)
                self.emit_status(agent["id"])

    def _release_routine(self, run: dict) -> None:
        """A routine may fire again once its run is over; only a run still waiting for the person keeps it busy."""
        rid = run.get("routine_id")
        if not rid:
            return
        status = (self.db.one("SELECT status FROM runs WHERE id=?", (run["id"],)) or {}).get("status")
        if status in ("waiting", "running", "queued"):
            return
        r = self.db.one("SELECT running, last_result FROM routines WHERE id=?", (rid,))
        if r and r["running"]:
            self.db.update("routines", "id", rid, {"running": 0, "last_result": r["last_result"] or (status or "")})
            self.bus.emit("routines", {"agent_id": run["agent_id"]})

    async def _loop(self, agent: dict, run: dict, state: dict, resume: list[dict] | None) -> None:
        watch = bool(state.get("watch"))
        tools = self.tools_for(agent, watch=watch)
        specs = [t.spec() for t in tools.values()]
        if state.get("seed") is not None:
            seed = state.pop("seed")
            history = self._history(agent["id"])
            # The message this run answers goes last, even if it was posted while an earlier run was still replying.
            for k in range(len(history) - 1, -1, -1):
                if history[k]["role"] == "user" and history[k].get("content") == seed:
                    del history[k]
                    break
            history.append({"role": "user", "content": seed})
            state["messages"] = history
        messages: list[dict] = state["messages"]
        ctx = ToolCtx(self, agent, run)

        # Resume: finish the tool calls that were queued behind the one we paused on.
        for call in resume or []:
            out = await self._handle_call(ctx, tools, call, state)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": out})

        started = time.time()
        max_steps = self.db.get_setting("max_steps", self.cfg.max_steps)
        while True:
            if run["id"] in self.stop_flags:
                self._save(run, state, status="stopped")
                self.post(agent["id"], "system", "⏹ Stopped.", run_id=run["id"])
                return
            steps = run.get("steps", 0)
            if steps >= max_steps or time.time() - started > self.cfg.turn_timeout_s:
                specs = []  # one last call without tools: wrap up
            if self._over_budget(agent):
                self._save(run, state, status="budget")
                self.post(agent["id"], "system", f"⚠️ {agent['name']} reached today's token budget.", run_id=run["id"])
                return
            system = {"role": "system", "content": self._system(agent, run, list(tools))}
            if watch:
                system["content"] += "\n\nThis is a WATCH run: you can only look. If nothing needs the person's attention, reply exactly NOTHING_NEW."
            res = await self.llm.chat(self.model_for(agent), [system, *self._trim(messages)], specs or None)
            self._usage(agent["id"], res.tokens)
            run["steps"] = steps + 1
            if res.tool_calls and specs:
                calls = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in res.tool_calls]
                messages.append({"role": "assistant", "content": res.content or "", "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls]})
                self._save(run, state, steps=run["steps"])
                for i, call in enumerate(calls):
                    try:
                        out = await self._handle_call(ctx, tools, call, state)
                    except Paused:
                        state["queued_calls"] = calls[i + 1:]
                        self._save(run, state, status="waiting")
                        raise
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": out})
                    self._save(run, state)
                continue
            text = res.content.strip() or "(done)"
            nudge = self._honesty_check(text, state, messages, tools) if specs else None
            if nudge:
                # Small local models often *say* they did something instead of calling the tool. Catch it once.
                state["nudged"] = True
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": nudge})
                self._save(run, state)
                continue
            messages.append({"role": "assistant", "content": text})
            self._save(run, state, status="done", steps=run["steps"])
            if not (watch and QUIET.match(text)):
                self.post(agent["id"], "agent", text, {"origin": run["origin"]}, run_id=run["id"])
            last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
            if specs and not state.get("tool_runs") and CLAIM.search(text) and not last_user.startswith("[Reply from coworker"):
                self.post(agent["id"], "system", "⚠️ That reply describes actions, but no tool ran in this task, so nothing was actually done.", {"unverified": True}, run_id=run["id"])
            await self._finish(agent, run, text)
            return

    def _honesty_check(self, text: str, state: dict, messages: list[dict], tools: dict) -> str | None:
        """Small local models often say they did something instead of calling the tool. Nudge once, specifically."""
        if state.get("nudged"):
            return None
        first_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if first_user.startswith("[Reply from coworker"):
            return None  # summarising a coworker's report isn't claiming its own actions
        ran = set(state.get("tool_runs") or [])
        hints = []
        url = URL_RE.search(first_user)
        if url and "web_fetch" in tools and not ran & {"web_fetch", "browser_open", "http_request"}:
            hints.append(f'You have not opened the link yet. Call web_fetch with {{"url": "{url.group(0).rstrip(".,)")}"}} and answer from what it returns.')
        if "remember" in tools and "remember" not in ran and LASTING.search(first_user):
            hints.append("The person stated a lasting preference or fact. Call remember to save it, then reply briefly.")
        if not ran and CLAIM.search(text) and not hints:
            hints.append("You have not called any tool in this task, so nothing has actually been done. If the task needs an "
                         "action, call the right tool now. Otherwise answer without claiming you did anything.")
        return "[Tillu check] " + " ".join(hints) if hints else None

    async def _handle_call(self, ctx: ToolCtx, tools: dict[str, Tool], call: dict, state: dict) -> str:
        agent, run = ctx.agent, ctx.run
        tool = tools.get(call["name"])
        if not tool:
            return f"ERROR: no tool named {call['name']}. Available: {', '.join(tools)}"
        args = call["arguments"] if isinstance(call["arguments"], dict) else {}
        try:
            c = await tool.classify(args, ctx)
        except Exception as e:
            c = Classification("external", summary=f"{tool.name}({json.dumps(args)[:200]})", why=f"could not inspect the action ({e}), so it asks")
        rules = self.db.all("SELECT * FROM rules WHERE agent_id=? AND revoked_at IS NULL", (agent["id"],))
        d = decide(c, tool=tool.name, autonomy=("watch" if state.get("watch") else agent["autonomy"]), tainted=bool(run["tainted"]),
                   origin=run["origin"], rules=rules, shell_enabled=bool(agent["shell_enabled"]))
        if d.action == "block":
            self._audit(agent, run, tool.name, c, "blocked", False, 0)
            self.post(agent["id"], "activity", f"⛔ {c.summary or tool.name}", {"tool": tool.name, "blocked": True, "why": d.reason}, run_id=run["id"])
            return f"BLOCKED by the Action Firewall: {d.reason}. Do not retry; tell the person what you need."
        if d.action == "ask":
            prov = self._provenance(c, args, state, run)
            aid = self.db.insert("approvals", {
                "run_id": run["id"], "agent_id": agent["id"], "tool": tool.name, "args": json.dumps(args), "call_id": call["id"],
                "effect": c.effect, "target": c.target, "summary": c.summary or f"{tool.name}({json.dumps(args)[:300]})",
                "why": d.reason, "rule_eligible": int(d.rule_eligible), "provenance": prov, "created_at": now()})
            self.post(agent["id"], "approval", c.summary, {"approval_id": aid, "effect": c.effect, "why": d.reason, "tool": tool.name,
                                                             "target": c.target, "rule_eligible": d.rule_eligible, "provenance": prov}, run_id=run["id"])
            self._audit(agent, run, tool.name, c, "asked", True, 0)
            self.bus.emit("approvals", {"agent_id": agent["id"]})
            raise Paused()
        return await self._execute(ctx, tool, args, call, c, decision="rule" if d.rule_id else "auto", state=state)

    def _provenance(self, c: Classification, args: dict, state: dict, run: dict) -> str:
        """Did the person themselves name this destination, or did it appear from outside content?

        'you'     : the target (recipient, host, path, command) appears in something the person typed in this task
        'outside' : it doesn't, and the run has read outside content (a page, a webhook, a tool result) -> suspicious
        'agent'   : it doesn't, but nothing outside was read: the coworker chose it on its own
        """
        target = (c.target or "").strip().lower()
        if not target:
            return ""
        own = []
        for m in state.get("messages", []):
            if m["role"] != "user":
                continue
            txt = m.get("content") or ""
            if txt.startswith(("[Task from coworker", "[Reply from coworker", "[Tillu check]", "Routine “")) or "Webhook payload:" in txt:
                continue
            own.append(txt.lower())
        blob = "\n".join(own)
        key = target
        if "@" in target:
            key = target
        elif target.startswith("$ ") or len(target) > 80:
            key = target[:40]
        if key in blob or (key.split("/")[0].replace("www.", "") in blob if "." in key else False):
            return "you"
        return "outside" if run.get("tainted") else "agent"

    async def _execute(self, ctx: ToolCtx, tool: Tool, args: dict, call: dict, c: Classification, decision: str, state: dict | None = None) -> str:
        agent, run = ctx.agent, ctx.run
        t0 = time.time()
        ok = True
        try:
            out = await tool.run(args, ctx)
        except PauseForUser as q:
            qid = self.db.insert("questions", {"run_id": run["id"], "agent_id": agent["id"], "call_id": call["id"], "question": q.question,
                                               "options": json.dumps(q.options), "created_at": now()})
            self.post(agent["id"], "question", q.question, {"question_id": qid, "options": q.options}, run_id=run["id"])
            self.bus.emit("approvals", {"agent_id": agent["id"]})
            raise Paused()
        except Exception as e:
            ok, out = False, f"ERROR: {type(e).__name__}: {e}"
        ms = int((time.time() - t0) * 1000)
        if ok and state is not None:
            state.setdefault("tool_runs", []).append(tool.name)
        if c.taints and not run["tainted"]:
            run["tainted"] = 1
            self.db.update("runs", "id", run["id"], {"tainted": 1, "taint_reason": c.summary[:200]})
        self._audit(agent, run, tool.name, c, decision, ok, ms)
        quiet = json.loads(run.get("state") or "{}").get("watch") if isinstance(run.get("state"), str) else False
        if tool.name not in ("remember", "forget", "ask_user", "browser_screenshot") and not quiet:
            self.post(agent["id"], "activity", c.summary or tool.name, {"tool": tool.name, "ok": ok, "ms": ms, "effect": c.effect,
                                                                         "decision": decision, "preview": str(out)[:400]}, run_id=run["id"])
        return str(out)

    def _audit(self, agent, run, tool, c: Classification, decision, ok, ms) -> None:
        self.db.insert("audit", {"agent_id": agent["id"], "run_id": run["id"], "tool": tool, "summary": (c.summary or "")[:500],
                                 "effect": c.effect, "decision": decision, "ok": int(ok), "ms": ms, "created_at": now()})

    def _usage(self, agent_id: str, tokens: int) -> None:
        if tokens:
            self.db.execute("INSERT INTO usage(agent_id, day, tokens) VALUES(?,?,?) ON CONFLICT(agent_id, day) DO UPDATE SET tokens=tokens+excluded.tokens",
                            (agent_id, dt.date.today().isoformat(), tokens))

    def _over_budget(self, agent: dict) -> bool:
        if not agent["daily_token_budget"]:
            return False
        row = self.db.one("SELECT tokens FROM usage WHERE agent_id=? AND day=?", (agent["id"], dt.date.today().isoformat()))
        return bool(row and row["tokens"] >= agent["daily_token_budget"])

    async def _finish(self, agent: dict, run: dict, text: str | None) -> None:
        if run.get("routine_id"):
            rid = run["routine_id"]
            r = self.db.one("SELECT * FROM routines WHERE id=?", (rid,))
            if r:
                self.db.update("routines", "id", rid, {"running": 0, "last_result": (text or run.get("status", ""))[:500]})
                self.bus.emit("routines", {"agent_id": agent["id"]})
        if run.get("parent_run_id") and text is not None:
            parent = self.db.one("SELECT * FROM runs WHERE id=?", (run["parent_run_id"],))
            if parent:
                self.post(parent["agent_id"], "relay_result", text, {"from": agent["id"], "from_name": agent["name"]})
                if run["hop"] < MAX_HOPS:
                    follow = self._new_run(parent["agent_id"], "relay", f"[Reply from coworker {agent['name']}]\n{text}", history=True,
                                           hop=run["hop"], tainted=bool(run["tainted"]), taint_reason="reply from a coworker whose run read outside content" if run["tainted"] else "")
                    self.spawn(self._drive(follow["id"]))

    # --- the person's decisions -------------------------------------------------------------------
    async def resolve_approval(self, approval_id: int, decision: str, note: str = "", rule_hours: float | None = None) -> dict:
        ap = self.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if not ap:
            raise KeyError("no such approval")
        if ap["status"] != "pending":
            raise ValueError(f"already {ap['status']}")
        approve = decision in ("approve", "always")
        self.db.update("approvals", "id", approval_id, {"status": "approved" if approve else "denied", "note": note, "resolved_at": now()})
        if decision == "always" and ap["rule_eligible"]:
            self.db.insert("rules", {"agent_id": ap["agent_id"], "tool": ap["tool"], "target": ap["target"],
                                     "expires_at": now() + rule_hours * 3600 if rule_hours else None, "created_at": now()})
        self.bus.emit("approvals", {"agent_id": ap["agent_id"], "resolved": approval_id})
        self.post(ap["agent_id"], "decision", ("✅ Approved" if approve else "✋ Denied") + (f": {note}" if note else ""),
                  {"approval_id": approval_id, "approved": approve, "always": decision == "always"}, run_id=ap["run_id"])
        self.spawn(self._continue_after_approval(ap, approve, note))
        return {"ok": True}

    async def _continue_after_approval(self, ap: dict, approve: bool, note: str) -> None:
        run = self.db.one("SELECT * FROM runs WHERE id=?", (ap["run_id"],))
        agent = self.get_agent(ap["agent_id"])
        if not run or not agent:
            return
        state = json.loads(run["state"])
        tools = self.tools_for(agent, watch=bool(state.get("watch")))
        ctx = ToolCtx(self, agent, run)
        call = {"id": ap["call_id"], "name": ap["tool"], "arguments": json.loads(ap["args"])}
        tool = tools.get(ap["tool"])
        async with self.lock(agent["id"]):
            self.db.update("runs", "id", run["id"], {"status": "running"})
            self.emit_status(agent["id"])
            try:
                if approve and tool:
                    c = Classification(ap["effect"], target=ap["target"], summary=ap["summary"], taints=ap["tool"].startswith(("browser_", "mcp_", "http_", "web_")))
                    changed = await self._reverify(ctx, tool, call["arguments"], ap)
                    if changed:
                        # The thing you approved is not the thing that would run now (e.g. the page swapped the button). Ask again.
                        prov = self._provenance(changed, call["arguments"], state, run)
                        aid = self.db.insert("approvals", {
                            "run_id": run["id"], "agent_id": agent["id"], "tool": tool.name, "args": json.dumps(call["arguments"]), "call_id": call["id"],
                            "effect": changed.effect, "target": changed.target, "summary": changed.summary,
                            "why": f"changed since you approved it (was: {ap['summary'][:120]})", "rule_eligible": 0, "provenance": prov, "created_at": now()})
                        self.post(agent["id"], "approval", changed.summary, {"approval_id": aid, "effect": changed.effect, "tool": tool.name, "target": changed.target,
                                                                              "why": "⚠️ The action changed after you approved it. Please look again.", "rule_eligible": False,
                                                                              "provenance": prov, "reverify": True}, run_id=run["id"])
                        self._audit(agent, run, tool.name, changed, "re-asked", True, 0)
                        self.bus.emit("approvals", {"agent_id": agent["id"]})
                        raise Paused()
                    out = await self._execute(ctx, tool, call["arguments"], call, c, decision="approved", state=state)
                elif approve:
                    out = "ERROR: that tool is no longer available."
                else:
                    out = "DENIED by the person." + (f" Their note: {note}" if note else "") + " Do not retry this; adjust or ask what they want."
            except Paused:
                self._save(run, state, status="waiting")
                self.emit_status(agent["id"])
                return
            state["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": out})
            queued = state.pop("queued_calls", [])
            self._save(run, state, status="queued")
        await self._drive(run["id"], resume=queued)

    async def _reverify(self, ctx: ToolCtx, tool: Tool, args: dict, ap: dict) -> Classification | None:
        """Re-inspect the action right before it runs; return the new classification if it no longer matches."""
        try:
            c = await tool.classify(args, ctx)
        except Exception:
            return None
        norm = lambda s: " ".join((s or "").lower().split())  # noqa: E731
        if c.effect == ap["effect"] and norm(c.target) == norm(ap["target"]) and norm(c.summary) == norm(ap["summary"]):
            return None
        if c.effect == "blocked":
            return c
        rank = EFFECT_ORDER.index
        # A downgrade (e.g. an element that vanished) is harmless; an upgrade or a different target is not.
        if rank(c.effect) <= rank(ap["effect"]) and norm(c.target) == norm(ap["target"]) and c.effect not in ("money", "destructive"):
            return None
        return c

    async def answer_question(self, question_id: int, answer: str) -> dict:
        q = self.db.one("SELECT * FROM questions WHERE id=?", (question_id,))
        if not q:
            raise KeyError("no such question")
        if q["status"] != "pending":
            raise ValueError("already answered")
        self.db.update("questions", "id", question_id, {"status": "answered", "answer": answer})
        self.post(q["agent_id"], "answer", answer, {"question_id": question_id}, run_id=q["run_id"])
        run = self.db.one("SELECT * FROM runs WHERE id=?", (q["run_id"],))
        state = json.loads(run["state"])
        state["messages"].append({"role": "tool", "tool_call_id": q["call_id"], "content": f"The person answered: {answer}"})
        state.setdefault("tool_runs", []).append("ask_user")
        queued = state.pop("queued_calls", [])
        self._save(run, state, status="queued")
        self.bus.emit("approvals", {"agent_id": q["agent_id"]})
        self.spawn(self._drive(run["id"], resume=queued))
        return {"ok": True}

    def stop(self, agent_id: str) -> int:
        n = 0
        for r in self.db.all("SELECT id FROM runs WHERE agent_id=? AND status IN ('running','queued')", (agent_id,)):
            self.stop_flags.add(r["id"])
            n += 1
        # Waiting runs are cancelled outright: their pending cards are withdrawn.
        for r in self.db.all("SELECT * FROM runs WHERE agent_id=? AND status='waiting'", (agent_id,)):
            self.db.update("runs", "id", r["id"], {"status": "stopped"})
            self.db.execute("UPDATE approvals SET status='withdrawn', resolved_at=? WHERE run_id=? AND status='pending'", (now(), r["id"]))
            self.db.execute("UPDATE questions SET status='withdrawn' WHERE run_id=? AND status='pending'", (r["id"],))
            self._release_routine(r)
            n += 1
        self.bus.emit("approvals", {"agent_id": agent_id})
        self.emit_status(agent_id)
        return n

    # --- delegation -------------------------------------------------------------------------------
    async def delegate(self, ctx: ToolCtx, to: str, task: str) -> str:
        target = self.find_agent(to)
        if not target:
            names = ", ".join(a["name"] for a in self.db.all("SELECT name FROM agents"))
            return f"No coworker called {to}. Coworkers: {names}"
        if target["id"] == ctx.agent["id"]:
            return "That's you."
        if ctx.run["hop"] >= MAX_HOPS:
            return f"Hand-off limit reached ({MAX_HOPS} hops): finish this yourself or ask the person."
        self.post(target["id"], "relay", task, {"from": ctx.agent["id"], "from_name": ctx.agent["name"]})
        run = self._new_run(target["id"], "relay", f"[Task from coworker {ctx.agent['name']}]\n{task}", history=True, hop=ctx.run["hop"] + 1,
                            parent_run_id=ctx.run["id"], tainted=bool(ctx.run["tainted"]),
                            taint_reason="handed over from a run that read outside content" if ctx.run["tainted"] else "")
        self.spawn(self._drive(run["id"]))
        return f"Handed to {target['name']}. Their reply will come back to you; you can finish your turn now."

    # --- routines ---------------------------------------------------------------------------------
    def create_routine(self, agent_id: str, *, name: str, cron: str, instructions: str, mode: str = "act", webhook: bool = False) -> dict:
        cron = cron.strip()
        if cron and not croniter.is_valid(cron):
            raise ValueError(f"invalid cron expression: {cron}")
        if not cron and not webhook:
            raise ValueError("give a cron schedule or webhook=true")
        secret = secrets.token_urlsafe(18) if webhook else None
        nxt = croniter(cron, dt.datetime.now()).get_next(float) if cron else None
        rid = self.db.insert("routines", {"agent_id": agent_id, "name": name[:80], "cron": cron, "instructions": instructions,
                                          "mode": mode if mode in ("act", "watch") else "act", "webhook_secret": secret,
                                          "next_run_at": nxt, "created_at": now()})
        r = self.db.one("SELECT * FROM routines WHERE id=?", (rid,))
        r["human"] = describe_cron(cron) if cron else "when its webhook is called"
        if secret:
            r["webhook_url"] = f"http://{self.cfg.public_host}:{self.cfg.port}/hook/{secret}"
        self.post(agent_id, "system", f"⏰ New routine “{r['name']}” — {r['human']}" + (" (watch: read-only, quiet unless something changes)" if r["mode"] == "watch" else ""))
        self.bus.emit("routines", {"agent_id": agent_id})
        return r

    def fire_routine(self, routine: dict, payload: str | None = None) -> dict | None:
        if routine["running"]:
            return None  # never overlap
        self.db.update("routines", "id", routine["id"], {"running": 1, "last_run_at": now()})
        text = f"Routine “{routine['name']}”: {routine['instructions']}"
        origin, tainted = "routine", False
        if payload is not None:
            origin, tainted = "webhook", True
            text += f"\n\nWebhook payload:\n<untrusted_tool_output>\n{payload[:8000]}\n</untrusted_tool_output>"
        if routine["mode"] != "watch":
            self.post(routine["agent_id"], "system", f"⏰ {'Webhook' if payload is not None else 'Routine'}: {routine['name']}")
        run = self._new_run(routine["agent_id"], origin, text, history=False, routine_id=routine["id"], tainted=tainted,
                            taint_reason="webhook payload" if tainted else "", watch=routine["mode"] == "watch")
        self.spawn(self._drive(run["id"]))
        self.bus.emit("routines", {"agent_id": routine["agent_id"]})
        return run

    async def _schedule_loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            await asyncio.sleep(15)

    def tick(self, at: float | None = None) -> int:
        t = at or time.time()
        fired = 0
        for r in self.db.all("SELECT * FROM routines WHERE enabled=1 AND cron != '' AND next_run_at IS NOT NULL AND next_run_at <= ?", (t,)):
            nxt = croniter(r["cron"], dt.datetime.fromtimestamp(t)).get_next(float)
            self.db.update("routines", "id", r["id"], {"next_run_at": nxt})
            if self.fire_routine(r):
                fired += 1
        return fired
