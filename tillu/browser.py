"""A real local browser per coworker (Playwright, optional).

Each coworker gets its own persistent Chromium profile, so a site you log into once stays logged in.
Clicks and key presses are inspected before they happen: a button that says Send / Pay / Delete, or
anything that submits a form, goes through the Action Firewall like any other outgoing action.
Typing into a password field is refused outright: use ask_for_login and sign in yourself.
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from .firewall import Classification, classify_label
from .tools import S, PauseForUser, Tool, ToolCtx, clip, obj

try:  # optional dependency
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None

MARK_JS = r"""
() => {
  const sel = 'a[href], button, input, textarea, select, [role=button], [role=link], [role=tab], [role=menuitem], [contenteditable=true], summary, label[for]';
  const out = [];
  let n = 0;
  document.querySelectorAll('[data-tillu-id]').forEach(e => e.removeAttribute('data-tillu-id'));
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    n += 1;
    el.setAttribute('data-tillu-id', String(n));
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder || el.title || el.name || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    const type = el.tagName.toLowerCase() + (el.type ? ':' + el.type : '');
    out.push(`[${n}] ${type} "${label}"`);
    if (n >= 150) break;
  }
  return out.join('\n');
}
"""

INSPECT_JS = r"""
(el) => {
  const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 120);
  const form = el.form || el.closest('form');
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  const isSubmit = !!form && ((tag === 'button' && (type === '' || type === 'submit')) || (tag === 'input' && (type === 'submit' || type === 'image')));
  let formLabel = '';
  if (form) {
    const b = form.querySelector('button[type=submit], input[type=submit], button:not([type])');
    if (b) formLabel = (b.getAttribute('aria-label') || b.innerText || b.value || '').trim().slice(0, 80);
  }
  const action = form ? (form.getAttribute('action') || location.href) : '';
  return { label, isSubmit, inForm: !!form, formLabel, action, password: type === 'password' || /pass(word)?|pin|cvv|cvc|card.?number/i.test(el.name || el.id || el.autocomplete || ''), host: location.host };
}
"""


class BrowserManager:
    def __init__(self, root: Path, headless: bool = True):
        self.root = root
        self.headless = headless
        self._pw = None
        self._ctx: dict[str, object] = {}
        self._headed: dict[str, bool] = {}
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return async_playwright is not None

    async def page(self, agent_id: str, headed: bool | None = None):
        async with self._lock:
            want_headed = bool(headed) if headed is not None else self._headed.get(agent_id, not self.headless)
            ctx = self._ctx.get(agent_id)
            if ctx is not None and self._headed.get(agent_id) != want_headed:
                await ctx.close()
                ctx = None
            if ctx is None:
                if self._pw is None:
                    self._pw = await async_playwright().start()
                profile = self.root / agent_id
                profile.mkdir(parents=True, exist_ok=True)
                ctx = await self._pw.chromium.launch_persistent_context(str(profile), headless=not want_headed, viewport={"width": 1280, "height": 860})
                self._ctx[agent_id] = ctx
                self._headed[agent_id] = want_headed
            pages = ctx.pages
            return pages[-1] if pages else await ctx.new_page()

    async def close(self):
        for c in self._ctx.values():
            try:
                await c.close()
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()


async def _snapshot(page) -> str:
    marks = await page.evaluate(MARK_JS)
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    return (f"URL: {page.url}\nTitle: {await page.title()}\n<untrusted_web_content>\n{clip(' '.join(text.split()), 4000)}\n"
            f"</untrusted_web_content>\nInteractive elements (use the number with browser_click / browser_type):\n{marks}")


async def _inspect(ctx: ToolCtx, el_id) -> dict | None:
    page = await ctx.app.browser.page(ctx.agent["id"])
    loc = page.locator(f'[data-tillu-id="{int(el_id)}"]').first
    if await loc.count() == 0:
        return None
    return await loc.evaluate(INSPECT_JS)


def browser_tools(mgr: BrowserManager) -> dict[str, Tool]:
    async def c_open(a, ctx):
        url = str(a.get("url", ""))
        return Classification("browse", target=url, summary=f"open {url}", taints=True)

    async def r_open(a, ctx):
        page = await mgr.page(ctx.agent["id"])
        await page.goto(str(a["url"]), wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(800)
        return await _snapshot(page)

    async def c_read(a, ctx):
        return Classification("browse", summary="read the current page", taints=True)

    async def r_read(a, ctx):
        return await _snapshot(await mgr.page(ctx.agent["id"]))

    async def c_click(a, ctx):
        info = await _inspect(ctx, a.get("element"))
        if not info:
            return Classification("browse", summary=f"click [{a.get('element')}]")
        effect, why = classify_label(info["label"], in_form=info["inForm"], is_submit=info["isSubmit"])
        return Classification(effect, target=info["host"], summary=f"click “{info['label'] or a.get('element')}” on {info['host']}", why=why, taints=True)

    async def r_click(a, ctx):
        page = await mgr.page(ctx.agent["id"])
        loc = page.locator(f'[data-tillu-id="{int(a["element"])}"]').first
        if await loc.count() == 0:
            return "That element number is stale: call browser_read to get fresh numbers."
        await loc.click(timeout=10000)
        await page.wait_for_timeout(1000)
        return await _snapshot(page)

    async def c_type(a, ctx):
        info = await _inspect(ctx, a.get("element"))
        if info and info["password"]:
            return Classification("blocked", summary="type into a password/payment field",
                                  why="coworkers never type passwords or card numbers: use ask_for_login and sign in yourself")
        if info and a.get("submit"):
            effect, why = classify_label(info["formLabel"] or info["label"], in_form=True, is_submit=True)
            return Classification(effect, target=info["host"], summary=f"type “{str(a.get('text',''))[:200]}” and submit on {info['host']}", why=why, taints=True)
        return Classification("browse", summary=f"type into [{a.get('element')}]", taints=True)

    async def r_type(a, ctx):
        page = await mgr.page(ctx.agent["id"])
        loc = page.locator(f'[data-tillu-id="{int(a["element"])}"]').first
        if await loc.count() == 0:
            return "That element number is stale: call browser_read to get fresh numbers."
        await loc.fill(str(a.get("text", "")), timeout=10000)
        if a.get("submit"):
            await loc.press("Enter")
            await page.wait_for_timeout(1200)
        return await _snapshot(page)

    async def c_press(a, ctx):
        key = str(a.get("key", ""))
        if key.lower() == "enter":
            page = await mgr.page(ctx.agent["id"])
            info = await page.evaluate("() => { const el = document.activeElement; const f = el && (el.form || el.closest('form')); if (!f) return null; const b = f.querySelector('button[type=submit], input[type=submit], button:not([type])'); return {label: b ? (b.innerText || b.value || '') : '', host: location.host}; }")
            if info:
                effect, why = classify_label(info["label"], in_form=True, is_submit=True)
                return Classification(effect, target=info["host"], summary=f"press Enter (submits a form on {info['host']})", why=why, taints=True)
        return Classification("browse", summary=f"press {key}", taints=True)

    async def r_press(a, ctx):
        page = await mgr.page(ctx.agent["id"])
        await page.keyboard.press(str(a.get("key", "Enter")))
        await page.wait_for_timeout(800)
        return await _snapshot(page)

    async def r_scroll(a, ctx):
        page = await mgr.page(ctx.agent["id"])
        await page.mouse.wheel(0, 700 if a.get("direction", "down") == "down" else -700)
        await page.wait_for_timeout(500)
        return await _snapshot(page)

    async def r_shot(a, ctx):
        page = await mgr.page(ctx.agent["id"])
        png = await page.screenshot(type="png")
        shots = ctx.app.cfg.data_dir / "shots"
        shots.mkdir(exist_ok=True)
        name = f"{ctx.agent['id']}-{int(asyncio.get_running_loop().time() * 1000)}.png"
        (shots / name).write_bytes(png)
        ctx.app.post(ctx.agent["id"], "screenshot", f"/shots/{name}", {"url": page.url}, run_id=ctx.run["id"])
        return f"Screenshot posted to the chat ({page.url})."

    async def r_login(a, ctx):
        page = await mgr.page(ctx.agent["id"], headed=True)
        if a.get("url"):
            await page.goto(str(a["url"]), wait_until="domcontentloaded", timeout=45000)
        raise PauseForUser(f"Please sign in to {a.get('site') or a.get('url')} in the browser window that just opened "
                           f"({a.get('why','')}). Your login stays in this coworker's own browser profile.", ["I'm signed in", "Skip"])

    async def c_read_only(a, ctx):
        return Classification("browse", summary="browser", taints=True)

    T = Tool
    el = {"type": "integer", "description": "element number from the page snapshot"}
    return {t.name: t for t in [
        T("browser_open", "browser", "Open a URL in your browser and get the page text plus numbered interactive elements.", obj({"url": S}, ["url"]), c_open, r_open),
        T("browser_read", "browser", "Re-read the current page (fresh element numbers).", obj({}), c_read, r_read),
        T("browser_click", "browser", "Click an element by number.", obj({"element": el}, ["element"]), c_click, r_click),
        T("browser_type", "browser", "Type text into an input by number. submit=true presses Enter after.", obj({"element": el, "text": S, "submit": {"type": "boolean"}}, ["element", "text"]), c_type, r_type),
        T("browser_press", "browser", "Press a key (Enter, Escape, Tab, ArrowDown...).", obj({"key": S}, ["key"]), c_press, r_press),
        T("browser_scroll", "browser", "Scroll the page up or down.", obj({"direction": {"type": "string", "enum": ["up", "down"]}}), c_read_only, r_scroll),
        T("browser_screenshot", "browser", "Post a screenshot of the page to the chat as evidence.", obj({}), c_read_only, r_shot),
        T("ask_for_login", "browser", "At a login wall, ask the person to sign in themselves in your browser window. Never ask for passwords.",
          obj({"site": S, "url": S, "why": S}, ["site"]), c_read_only, r_login),
    ]}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
