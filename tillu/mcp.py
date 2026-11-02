"""Minimal MCP client for local stdio servers (filesystem, git, sqlite, your own tools...).

Tools are exposed to coworkers as ``mcp_<server>__<tool>``. The firewall uses the server's own
annotations: readOnlyHint -> browse (untrusted output), destructiveHint -> destructive, otherwise
external (asks). You can mark specific tools as safe reads per server in Settings.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field

from .firewall import Classification
from .tools import Tool, ToolCtx, clip

PROTOCOL = "2025-06-18"


@dataclass
class MCPServer:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    safe_tools: list[str] = field(default_factory=list)
    proc: asyncio.subprocess.Process | None = None
    tools: list[dict] = field(default_factory=list)
    error: str = ""
    _id: int = 0
    _pending: dict = field(default_factory=dict)
    _reader: asyncio.Task | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            self.command, *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env={**os.environ, **self.env}, limit=16 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read())
        await self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "tillu", "version": "0.1"}})
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        res = await self.request("tools/list", {})
        self.tools = res.get("tools", [])

    async def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            fut = self._pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                fut.set_result(msg)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(f"MCP server {self.name} exited"))

    async def request(self, method: str, params: dict, timeout: float = 60) -> dict:
        async with self._lock:
            self._id += 1
            rid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        msg = await asyncio.wait_for(fut, timeout)
        if "error" in msg:
            raise RuntimeError(msg["error"].get("message", "MCP error"))
        return msg.get("result", {})

    async def call(self, tool: str, args: dict) -> str:
        res = await self.request("tools/call", {"name": tool, "arguments": args}, timeout=180)
        parts = []
        for c in res.get("content", []):
            parts.append(c.get("text", "") if c.get("type") == "text" else f"[{c.get('type')} content]")
        text = "\n".join(parts)
        return ("ERROR: " if res.get("isError") else "") + text

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        if self._reader:
            self._reader.cancel()


def _safe_name(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", s)[:40]


class MCPManager:
    def __init__(self) -> None:
        self.servers: dict[str, MCPServer] = {}

    async def load(self, configs: list[dict]) -> None:
        await self.stop()
        for c in configs or []:
            s = MCPServer(name=_safe_name(c["name"]), command=c["command"], args=list(c.get("args", [])),
                          env=dict(c.get("env", {})), safe_tools=list(c.get("safe_tools", [])))
            try:
                await asyncio.wait_for(s.start(), 30)
            except Exception as e:  # keep the app up if one server is broken
                s.error = str(e) or type(e).__name__
                await s.stop()
            self.servers[s.name] = s

    async def stop(self) -> None:
        for s in self.servers.values():
            await s.stop()
        self.servers = {}

    def status(self) -> list[dict]:
        return [{"name": s.name, "tools": [t["name"] for t in s.tools], "error": s.error} for s in self.servers.values()]

    def tools(self) -> dict[str, Tool]:
        out: dict[str, Tool] = {}
        for s in self.servers.values():
            if s.error:
                continue
            for t in s.tools:
                out_name = f"mcp_{s.name}__{_safe_name(t['name'])}"
                out[out_name] = self._wrap(s, t, out_name)
        return out

    def _wrap(self, s: MCPServer, t: dict, name: str) -> Tool:
        ann = t.get("annotations") or {}

        async def classify(args: dict, ctx: ToolCtx) -> Classification:
            summary = f"{s.name}.{t['name']}({json.dumps(args)[:300]})"
            if t["name"] in s.safe_tools or ann.get("readOnlyHint"):
                return Classification("browse", target=s.name, summary=summary, taints=True)
            if ann.get("destructiveHint"):
                return Classification("destructive", target=s.name, summary=summary, why=f"{s.name} marks {t['name']} as destructive")
            return Classification("external", target=f"{s.name}.{t['name']}", summary=summary, why=f"calls {t['name']} on MCP server {s.name}", taints=True)

        async def run(args: dict, ctx: ToolCtx) -> str:
            return "<untrusted_tool_output>\n" + clip(await s.call(t["name"], args)) + "\n</untrusted_tool_output>"

        params = t.get("inputSchema") or {"type": "object", "properties": {}}
        return Tool(name, "mcp", (t.get("description") or t["name"])[:500], params, classify, run)
