"""HTTP API + web UI. Binds to 127.0.0.1 and requires the local token for everything except webhooks."""
from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse

from .config import Config
from .db import now
from .firewall import AUTONOMY_LEVELS
from .runtime import Runtime, describe_cron
from .templates import TEMPLATES
from .tools import GROUPS

WEB = Path(__file__).parent / "web"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def create_app(cfg: Config, runtime: Runtime | None = None) -> FastAPI:
    rt = runtime or Runtime(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await rt.start()
        yield
        await rt.shutdown()

    app = FastAPI(title="Agent Tillu", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.rt = rt

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if cfg.host in LOCAL_HOSTS and host not in LOCAL_HOSTS:
            return PlainTextResponse("Agent Tillu only answers on localhost.", status_code=403)  # DNS-rebinding guard
        if path.startswith("/hook/") or path.startswith("/static/") or path == "/":
            return await call_next(request)
        token = request.cookies.get("tillu_token") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(token or "", cfg.token):
            return JSONResponse({"error": "unauthorized: open the link Agent Tillu printed when it started"}, status_code=401)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-tillu") != "1":
            return JSONResponse({"error": "missing X-Tillu header"}, status_code=403)  # CSRF guard
        return await call_next(request)

    # --- pages ------------------------------------------------------------------------------------
    @app.get("/")
    async def index(request: Request, token: str | None = None):
        if token:
            if not secrets.compare_digest(token, cfg.token):
                return PlainTextResponse("Wrong token.", status_code=401)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie("tillu_token", token, httponly=True, samesite="strict", max_age=3600 * 24 * 365)
            return resp
        if not secrets.compare_digest(request.cookies.get("tillu_token") or "", cfg.token):
            return HTMLResponse("<body style='font:16px system-ui;padding:40px'><h2>Agent Tillu</h2><p>Open the sign-in link that Agent Tillu printed in your terminal when it started.</p></body>", status_code=401)
        return FileResponse(WEB / "index.html")

    @app.get("/static/{name}")
    async def static(name: str):
        f = (WEB / name).resolve()
        if WEB.resolve() not in f.parents or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f)

    @app.get("/shots/{name}")
    async def shots(name: str):
        f = (cfg.data_dir / "shots" / name).resolve()
        if (cfg.data_dir / "shots").resolve() not in f.parents or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f)

    # --- state ------------------------------------------------------------------------------------
    def agent_view(a: dict) -> dict:
        a = dict(a)
        a["tools"] = json.loads(a["tools"])
        a["state"] = rt.agent_state(a["id"])
        a["pending"] = rt.db.one("SELECT (SELECT COUNT(*) FROM approvals WHERE agent_id=? AND status='pending') + "
                                 "(SELECT COUNT(*) FROM questions WHERE agent_id=? AND status='pending') AS n", (a["id"], a["id"]))["n"]
        last = rt.db.one("SELECT role, content, created_at FROM messages WHERE agent_id=? AND role IN ('agent','user','system','approval','question','relay','relay_result') ORDER BY id DESC LIMIT 1", (a["id"],))
        a["last"] = last
        a["model_effective"] = rt.model_for(a)
        return a

    @app.get("/api/state")
    async def state():
        agents = [agent_view(a) for a in rt.db.all("SELECT * FROM agents ORDER BY is_foreman DESC, created_at")]
        return {
            "agents": agents,
            "templates": {k: {"name": v["name"], "role": v["role"], "emoji": v["emoji"], "groups": v["groups"]} for k, v in TEMPLATES.items() if k != "foreman"},
            "groups": GROUPS, "autonomy": AUTONOMY_LEVELS,
            "browser": rt.browser.available, "mcp": rt.mcp.status(),
            "default_model": rt.db.get_setting("default_model", cfg.default_model),
        }

    # --- agents -----------------------------------------------------------------------------------
    @app.get("/api/agents/{agent_id}/messages")
    async def messages(agent_id: str, before: int | None = None, limit: int = 200):
        q = "SELECT * FROM messages WHERE agent_id=?" + (" AND id < ?" if before else "") + " ORDER BY id DESC LIMIT ?"
        rows = rt.db.all(q, (agent_id, before, limit) if before else (agent_id, limit))
        for r in rows:
            r["meta"] = json.loads(r["meta"])
            if r["role"] == "approval":
                ap = rt.db.one("SELECT status FROM approvals WHERE id=?", (r["meta"].get("approval_id"),))
                r["meta"]["status"] = ap["status"] if ap else "gone"
            if r["role"] == "question":
                qq = rt.db.one("SELECT status, answer FROM questions WHERE id=?", (r["meta"].get("question_id"),))
                r["meta"]["status"] = qq["status"] if qq else "gone"
        return list(reversed(rows))

    @app.post("/api/agents/{agent_id}/messages")
    async def send(agent_id: str, body: dict):
        if not rt.get_agent(agent_id):
            raise HTTPException(404)
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(400, "empty message")
        return await rt.send(agent_id, text)

    @app.post("/api/agents/{agent_id}/stop")
    async def stop(agent_id: str):
        return {"stopped": rt.stop(agent_id)}

    @app.post("/api/agents")
    async def hire(body: dict):
        a = rt.hire(name=str(body.get("name", "")), role=str(body.get("role", "")), template=str(body.get("template", "")),
                    instructions=str(body.get("instructions", "")), tools=body.get("tools"), model=str(body.get("model", "")))
        return agent_view(a)

    @app.patch("/api/agents/{agent_id}")
    async def edit_agent(agent_id: str, body: dict):
        if not rt.get_agent(agent_id):
            raise HTTPException(404)
        allowed = {"name", "role", "soul", "model", "tools", "autonomy", "shell_enabled", "emoji", "daily_token_budget"}
        changes = {k: v for k, v in body.items() if k in allowed}
        if "tools" in changes:
            changes["tools"] = json.dumps([g for g in changes["tools"] if g in GROUPS])
        if "autonomy" in changes and changes["autonomy"] not in AUTONOMY_LEVELS:
            raise HTTPException(400, "bad autonomy")
        if "shell_enabled" in changes:
            changes["shell_enabled"] = int(bool(changes["shell_enabled"]))
        rt.db.update("agents", "id", agent_id, changes)
        rt.bus.emit("agents", {"id": agent_id})
        return agent_view(rt.get_agent(agent_id))

    @app.delete("/api/agents/{agent_id}")
    async def fire(agent_id: str):
        a = rt.get_agent(agent_id)
        if not a:
            raise HTTPException(404)
        rt.stop(agent_id)
        for t in ("messages", "runs", "approvals", "questions", "rules", "routines", "memories", "memory_journal", "audit", "usage"):
            rt.db.execute(f"DELETE FROM {t} WHERE agent_id=?", (agent_id,))
        rt.db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        rt.bus.emit("agents", {"id": agent_id, "deleted": True})
        return {"ok": True}

    # --- inbox / approvals / questions ------------------------------------------------------------
    @app.get("/api/inbox")
    async def inbox():
        aps = rt.db.all("SELECT a.*, g.name AS agent_name, g.emoji FROM approvals a JOIN agents g ON g.id=a.agent_id WHERE a.status='pending' ORDER BY a.id")
        qs = rt.db.all("SELECT q.*, g.name AS agent_name, g.emoji FROM questions q JOIN agents g ON g.id=q.agent_id WHERE q.status='pending' ORDER BY q.id")
        for q in qs:
            q["options"] = json.loads(q["options"])
        recent = rt.db.all("SELECT r.id, r.name, r.last_result, r.last_run_at, g.name AS agent_name, g.emoji, g.id AS agent_id FROM routines r JOIN agents g ON g.id=r.agent_id "
                           "WHERE r.last_run_at IS NOT NULL ORDER BY r.last_run_at DESC LIMIT 10")
        stuck = [agent_view(a) for a in rt.db.all("SELECT * FROM agents") if rt.agent_state(a["id"]) == "stuck"]
        return {"approvals": aps, "questions": qs, "routines": recent, "stuck": stuck}

    @app.post("/api/approvals/{approval_id}")
    async def decide(approval_id: int, body: dict):
        decision = body.get("decision")
        if decision not in ("approve", "deny", "always"):
            raise HTTPException(400, "decision must be approve, deny or always")
        try:
            return await rt.resolve_approval(approval_id, decision, str(body.get("note", "")), body.get("hours"))
        except KeyError:
            raise HTTPException(404)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/questions/{question_id}")
    async def answer(question_id: int, body: dict):
        try:
            return await rt.answer_question(question_id, str(body.get("answer", "")).strip() or "(no answer)")
        except KeyError:
            raise HTTPException(404)
        except ValueError as e:
            raise HTTPException(409, str(e))

    # --- memory -----------------------------------------------------------------------------------
    @app.get("/api/agents/{agent_id}/memory")
    async def mem_list(agent_id: str):
        return {"memories": rt.memory.list(agent_id),
                "journal": rt.db.all("SELECT * FROM memory_journal WHERE agent_id=? ORDER BY id DESC LIMIT 50", (agent_id,))}

    @app.post("/api/agents/{agent_id}/memory")
    async def mem_add(agent_id: str, body: dict):
        m = await rt.memory.add(agent_id, str(body.get("content", "")), str(body.get("kind", "fact")), by="user")
        return m

    @app.patch("/api/agents/{agent_id}/memory/{mem_id}")
    async def mem_edit(agent_id: str, mem_id: int, body: dict):
        if "content" in body:
            rt.memory.edit(agent_id, mem_id, str(body["content"]))
        if "pinned" in body:
            rt.db.update("memories", "id", mem_id, {"pinned": int(bool(body["pinned"]))})
        return {"ok": True}

    @app.delete("/api/agents/{agent_id}/memory/{mem_id}")
    async def mem_del(agent_id: str, mem_id: int):
        rt.memory.remove(agent_id, mem_id, by="user")
        return {"ok": True}

    @app.get("/api/agents/{agent_id}/memory.md")
    async def mem_md(agent_id: str):
        return PlainTextResponse(rt.memory.as_markdown(agent_id), media_type="text/markdown")

    # --- routines ---------------------------------------------------------------------------------
    def routine_view(r: dict) -> dict:
        r = dict(r)
        r["human"] = describe_cron(r["cron"]) if r["cron"] else "on webhook"
        if r.get("webhook_secret"):
            r["webhook_url"] = f"http://{cfg.public_host}:{cfg.port}/hook/{r['webhook_secret']}"
        r.pop("webhook_secret", None)
        return r

    @app.get("/api/agents/{agent_id}/routines")
    async def routines(agent_id: str):
        return [routine_view(r) for r in rt.db.all("SELECT * FROM routines WHERE agent_id=? ORDER BY id", (agent_id,))]

    @app.post("/api/agents/{agent_id}/routines")
    async def routine_new(agent_id: str, body: dict):
        try:
            r = rt.create_routine(agent_id, name=str(body.get("name", "Routine")), cron=str(body.get("cron", "")),
                                  instructions=str(body.get("instructions", "")), mode=str(body.get("mode", "act")), webhook=bool(body.get("webhook")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return routine_view(rt.db.one("SELECT * FROM routines WHERE id=?", (r["id"],)))

    @app.patch("/api/routines/{rid}")
    async def routine_edit(rid: int, body: dict):
        r = rt.db.one("SELECT * FROM routines WHERE id=?", (rid,))
        if not r:
            raise HTTPException(404)
        changes = {}
        if "enabled" in body:
            changes["enabled"] = int(bool(body["enabled"]))
        rt.db.update("routines", "id", rid, changes)
        rt.bus.emit("routines", {"agent_id": r["agent_id"]})
        return routine_view(rt.db.one("SELECT * FROM routines WHERE id=?", (rid,)))

    @app.post("/api/routines/{rid}/run")
    async def routine_run(rid: int):
        r = rt.db.one("SELECT * FROM routines WHERE id=?", (rid,))
        if not r:
            raise HTTPException(404)
        run = rt.fire_routine(r)
        return {"started": bool(run)}

    @app.delete("/api/routines/{rid}")
    async def routine_del(rid: int):
        r = rt.db.one("SELECT agent_id FROM routines WHERE id=?", (rid,))
        rt.db.execute("DELETE FROM routines WHERE id=?", (rid,))
        if r:
            rt.bus.emit("routines", {"agent_id": r["agent_id"]})
        return {"ok": True}

    @app.post("/hook/{secret}")
    async def hook(secret: str, request: Request):
        r = rt.db.one("SELECT * FROM routines WHERE webhook_secret=? AND enabled=1", (secret,))
        if not r:
            raise HTTPException(404)
        body = (await request.body())[:20000].decode(errors="replace")
        run = rt.fire_routine(r, payload=body)
        return {"accepted": bool(run), "busy": not run}

    # --- rules & audit ----------------------------------------------------------------------------
    @app.get("/api/agents/{agent_id}/rules")
    async def rules(agent_id: str):
        return rt.db.all("SELECT * FROM rules WHERE agent_id=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) ORDER BY id DESC", (agent_id, now()))

    @app.delete("/api/rules/{rule_id}")
    async def revoke(rule_id: int):
        rt.db.update("rules", "id", rule_id, {"revoked_at": now()})
        return {"ok": True}

    @app.get("/api/audit")
    async def audit(agent_id: str | None = None, limit: int = 200):
        if agent_id:
            return rt.db.all("SELECT * FROM audit WHERE agent_id=? ORDER BY id DESC LIMIT ?", (agent_id, limit))
        return rt.db.all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/usage")
    async def usage():
        return rt.db.all("SELECT * FROM usage ORDER BY day DESC LIMIT 200")

    # --- settings ---------------------------------------------------------------------------------
    SETTING_KEYS = {"model_base_url": cfg.model_base_url, "model_api_key": cfg.model_api_key, "default_model": cfg.default_model,
                    "embed_model": cfg.embed_model, "max_steps": cfg.max_steps, "num_ctx": cfg.num_ctx, "browser_headless": True, "searxng_url": "",
                    "smtp": {}, "mcp_servers": []}

    @app.get("/api/settings")
    async def get_settings():
        out = {k: rt.db.get_setting(k, d) for k, d in SETTING_KEYS.items()}
        if out["smtp"].get("password"):
            out["smtp"] = {**out["smtp"], "password": "••••••"}
        return out

    @app.put("/api/settings")
    async def put_settings(body: dict):
        for k, v in body.items():
            if k not in SETTING_KEYS:
                continue
            if k == "smtp" and isinstance(v, dict) and v.get("password") == "••••••":
                v = {**v, "password": rt.db.get_setting("smtp", {}).get("password", "")}
            rt.db.set_setting(k, v)
        rt.apply_settings()
        if "mcp_servers" in body:
            await rt.mcp.load(rt.db.get_setting("mcp_servers", []))
        return await get_settings()

    @app.get("/api/models")
    async def models():
        try:
            return {"models": ["stub", *await rt.llm.list_models()], "ok": True}
        except Exception as e:
            return {"models": ["stub"], "ok": False, "error": f"Can't reach the model server at {rt.llm.base_url}: {e}"}

    @app.get("/api/export")
    async def export():
        tables = ["agents", "messages", "routines", "memories", "memory_journal", "rules", "audit"]
        data = {t: rt.db.all(f"SELECT * FROM {t}") for t in tables}
        for m in data["memories"]:
            m.pop("embedding", None)
        return JSONResponse(data, headers={"Content-Disposition": "attachment; filename=tillu-export.json"})

    # --- live events ------------------------------------------------------------------------------
    @app.get("/api/events")
    async def events(request: Request):
        q = rt.bus.subscribe()

        async def gen():
            try:
                yield "retry: 2000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(q.get(), 15)
                        yield f"data: {item}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                rt.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
