import sys
from pathlib import Path

import pytest

from tillu.llm import ChatResult, ToolCall
from conftest import settle

pytestmark = pytest.mark.asyncio
SERVER = str(Path(__file__).with_name("fake_mcp_server.py"))


async def test_mcp_tools_are_firewalled(rt):
    await rt.mcp.load([{"name": "fake", "command": sys.executable, "args": [SERVER]}])
    assert rt.mcp.status()[0]["error"] == ""
    a = rt.hire(name="Ops", role="ops", tools=["core", "mcp"])
    names = set(rt.tools_for(a))
    assert {"mcp_fake__get_weather", "mcp_fake__create_ticket", "mcp_fake__drop_table"} <= names
    script = [ChatResult("", [ToolCall("1", "mcp_fake__get_weather", {"city": "Pune"})]),
              ChatResult("", [ToolCall("2", "mcp_fake__create_ticket", {"title": "x"})]),
              ChatResult("done")]

    async def fake_chat(model, messages, tools=None, temperature=0.3):
        return script.pop(0)
    rt.llm.chat = fake_chat
    await rt.send(a["id"], "go")
    await settle(rt)
    audit = {r["tool"]: r for r in rt.db.all("SELECT * FROM audit WHERE agent_id=?", (a["id"],))}
    assert audit["mcp_fake__get_weather"]["decision"] == "auto"
    ap = rt.db.one("SELECT * FROM approvals WHERE agent_id=?", (a["id"],))
    assert ap["tool"] == "mcp_fake__create_ticket" and ap["effect"] == "external"
    run = rt.db.one("SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC", (a["id"],))
    assert run["tainted"] == 1  # read MCP output
    await rt.resolve_approval(ap["id"], "approve")
    await settle(rt)
    tool_msgs = [m for m in __import__("json").loads(rt.db.one("SELECT state FROM runs WHERE id=?", (run["id"],))["state"])["messages"] if m["role"] == "tool"]
    assert "create_ticket ok" in tool_msgs[-1]["content"]
    await rt.mcp.stop()
