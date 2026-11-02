import asyncio

import httpx
import pytest

from tillu.server import create_app
from conftest import settle

pytestmark = pytest.mark.asyncio
H = {"x-tillu": "1", "host": "127.0.0.1"}


@pytest.fixture
async def client(rt, cfg):
    app = create_app(cfg, rt)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c


async def test_auth_and_csrf(client):
    assert (await client.get("/api/state")).status_code == 401
    r = await client.get("/?token=test-token", follow_redirects=False)
    assert r.status_code == 303 and "tillu_token" in r.headers["set-cookie"]
    client.cookies.set("tillu_token", "test-token")
    assert (await client.get("/api/state")).status_code == 200
    assert (await client.post("/api/agents", json={"name": "x", "role": "y"})).status_code == 403  # no X-Tillu header
    assert (await client.get("/api/state", headers={"host": "evil.com"})).status_code == 403  # DNS rebinding


async def test_full_flow_over_http(client, rt):
    client.cookies.set("tillu_token", "test-token")
    st = (await client.get("/api/state")).json()
    assert st["agents"][0]["name"] == "Foreman" and "researcher" in st["templates"]
    a = (await client.post("/api/agents", json={"template": "executive-assistant", "name": "Ava", "role": ""}, headers=H)).json()
    assert a["role"].startswith("Executive")
    await client.post(f"/api/agents/{a['id']}/messages", json={"text": "email zoe@ex.com the report"}, headers=H)
    await settle(rt)
    inbox = (await client.get("/api/inbox")).json()
    assert len(inbox["approvals"]) == 1
    apid = inbox["approvals"][0]["id"]
    msgs = (await client.get(f"/api/agents/{a['id']}/messages")).json()
    assert [m for m in msgs if m["role"] == "approval"][0]["meta"]["status"] == "pending"
    r = await client.post(f"/api/approvals/{apid}", json={"decision": "always", "hours": 1}, headers=H)
    assert r.status_code == 200
    assert (await client.post(f"/api/approvals/{apid}", json={"decision": "approve"}, headers=H)).status_code == 409
    await settle(rt)
    rules = (await client.get(f"/api/agents/{a['id']}/rules")).json()
    assert rules and rules[0]["target"] == "zoe@ex.com" and rules[0]["expires_at"]
    await client.delete(f"/api/rules/{rules[0]['id']}", headers=H)
    assert (await client.get(f"/api/agents/{a['id']}/rules")).json() == []
    # memory CRUD
    m = (await client.post(f"/api/agents/{a['id']}/memory", json={"content": "Boss likes bullet points", "kind": "preference"}, headers=H)).json()
    mem = (await client.get(f"/api/agents/{a['id']}/memory")).json()
    assert mem["memories"][0]["content"] == "Boss likes bullet points"
    assert "bullet points" in (await client.get(f"/api/agents/{a['id']}/memory.md")).text
    await client.delete(f"/api/agents/{a['id']}/memory/{m['id']}", headers=H)
    # routines + webhook
    r = (await client.post(f"/api/agents/{a['id']}/routines", json={"name": "brief", "cron": "30 7 * * 1-5", "instructions": "brief me"}, headers=H)).json()
    assert r["human"] == "weekdays at 07:30"
    bad = await client.post(f"/api/agents/{a['id']}/routines", json={"name": "x", "cron": "not cron", "instructions": "y"}, headers=H)
    assert bad.status_code == 400
    w = (await client.post(f"/api/agents/{a['id']}/routines", json={"name": "hook", "webhook": True, "instructions": "triage"}, headers=H)).json()
    secret = w["webhook_url"].rsplit("/", 1)[1]
    hook = await client.post(f"/hook/{secret}", content=b'{"issue": "printer on fire"}', headers={"host": "127.0.0.1"})
    assert hook.json()["accepted"]
    await settle(rt)
    assert (await client.post("/hook/nope", headers={"host": "127.0.0.1"})).status_code == 404
    # settings never leak the SMTP password
    await client.put("/api/settings", json={"smtp": {"host": "smtp.x", "password": "hunter2"}}, headers=H)
    s = (await client.get("/api/settings")).json()
    assert s["smtp"]["password"] == "••••••"
    await client.put("/api/settings", json={"smtp": s["smtp"]}, headers=H)
    assert rt.db.get_setting("smtp")["password"] == "hunter2"
    assert (await client.get("/api/audit")).json()
    exp = (await client.get("/api/export")).json()
    assert "agents" in exp and "embedding" not in (exp["memories"][0] if exp["memories"] else {})
    assert (await client.delete(f"/api/agents/{a['id']}", headers=H)).json()["ok"]
