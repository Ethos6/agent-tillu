import asyncio
import http.server
import threading
from functools import partial

import pytest

from tillu.browser import async_playwright
from tillu.llm import ChatResult, ToolCall
from conftest import settle

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(async_playwright is None, reason="playwright not installed")]

PAGE = """<html><body><h1>Webmail</h1>
<a href="/inbox">Inbox</a>
<form action="/send" method="post"><input name="to" placeholder="To"><input type="password" name="password" placeholder="Password">
<textarea name="body" placeholder="Message"></textarea><button type="submit">Send</button></form>
<button onclick="document.body.dataset.deleted=1">Delete all</button>
<button type="button" onclick="document.title='next'">Next page</button>
</body></html>"""


@pytest.fixture
def site(tmp_path):
    (tmp_path / "index.html").write_text(PAGE)
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    srv.shutdown()


async def test_browser_clicks_are_classified(rt, site):
    a = rt.hire(name="Bea", role="browser", tools=["core", "browser"])
    script = [
        ChatResult("", [ToolCall("1", "browser_open", {"url": site})]),
        ChatResult("", [ToolCall("2", "browser_click", {"element": 7})]),   # Next page -> allowed
        ChatResult("", [ToolCall("3", "browser_type", {"element": 3, "text": "hunter2"})]),  # password -> blocked
        ChatResult("", [ToolCall("4", "browser_click", {"element": 6})]),   # Delete all -> destructive, asks
    ]

    async def fake_chat(model, messages, tools=None, temperature=0.3):
        return script.pop(0) if script else ChatResult("done")
    rt.llm.chat = fake_chat
    await rt.send(a["id"], "go")
    await settle(rt, timeout=60)
    state = __import__("json").loads(rt.db.one("SELECT state FROM runs WHERE agent_id=? ORDER BY id DESC", (a["id"],))["state"])
    snapshot = state["messages"][2]["content"]
    assert '[6] button:submit "Delete all"' in snapshot and '[3] input:password' in snapshot
    audit = rt.db.all("SELECT tool, effect, decision FROM audit WHERE agent_id=? ORDER BY id", (a["id"],))
    assert audit[0] == {"tool": "browser_open", "effect": "browse", "decision": "auto"}
    assert audit[1]["decision"] == "auto"
    assert audit[2]["effect"] == "blocked"
    ap = rt.db.one("SELECT * FROM approvals WHERE agent_id=?", (a["id"],))
    assert ap["effect"] == "destructive" and "Delete all" in ap["summary"]


async def test_submit_button_needs_approval(rt, site):
    a = rt.hire(name="Bea", role="browser", tools=["core", "browser"])
    script = [ChatResult("", [ToolCall("1", "browser_open", {"url": site})]),
              ChatResult("", [ToolCall("2", "browser_type", {"element": 2, "text": "x@y.z"})]),
              ChatResult("", [ToolCall("3", "browser_click", {"element": 5})])]

    async def fake_chat(model, messages, tools=None, temperature=0.3):
        return script.pop(0) if script else ChatResult("done")
    rt.llm.chat = fake_chat
    await rt.send(a["id"], "go")
    await settle(rt, timeout=60)
    ap = rt.db.one("SELECT * FROM approvals WHERE agent_id=?", (a["id"],))
    assert ap and ap["effect"] == "external" and "Send" in ap["summary"] and ap["target"].startswith("127.0.0.1")


async def test_action_is_reverified_at_dispatch(rt, site):
    """Approve a click on 'Delete all'; the page then swaps the button to 'Pay now'. It must ask again, not click."""
    a = rt.hire(name="Bea", role="browser", tools=["core", "browser"])
    script = [ChatResult("", [ToolCall("1", "browser_open", {"url": site})]),
              ChatResult("", [ToolCall("2", "browser_click", {"element": 6})])]

    async def fake_chat(model, messages, tools=None, temperature=0.3):
        return script.pop(0) if script else ChatResult("done")
    rt.llm.chat = fake_chat
    await rt.send(a["id"], "go")
    await settle(rt, timeout=60)
    ap = rt.db.one("SELECT * FROM approvals WHERE agent_id=?", (a["id"],))
    assert ap["effect"] == "destructive"
    page = await rt.browser.page(a["id"])
    await page.evaluate("() => { const b = document.querySelector('[data-tillu-id=\"6\"]'); b.textContent = 'Pay now'; }")
    await rt.resolve_approval(ap["id"], "approve")
    await settle(rt, timeout=60)
    aps = rt.db.all("SELECT * FROM approvals WHERE agent_id=? ORDER BY id", (a["id"],))
    assert len(aps) == 2 and aps[1]["status"] == "pending" and aps[1]["effect"] == "money" and "Pay now" in aps[1]["summary"]
    assert await page.evaluate("() => document.body.dataset.deleted || ''") == ""  # never clicked
    assert rt.db.one("SELECT * FROM audit WHERE agent_id=? AND decision='re-asked'", (a["id"],))
