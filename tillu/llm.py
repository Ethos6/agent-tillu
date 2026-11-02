"""Model client for any OpenAI-compatible server (Ollama, LM Studio, llama.cpp, vLLM, ...).

Local models are weaker at tool calling than frontier APIs, so this layer also:
  * repairs tool calls that arrive as text (``<tool_call>{...}</tool_call>``, fenced JSON, bare JSON)
  * strips ``<think>...</think>`` reasoning blocks before anything reaches the chat
  * retries once without tools if the server rejects the tools field
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    tokens: int = 0
    repaired: bool = False


THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
TOOL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
FENCE_RE = re.compile(r"```(?:json|tool_call)?\s*(\{.*?\})\s*```", re.S)


def strip_thinking(text: str | None) -> str:
    if not text:
        return ""
    text = THINK_RE.sub("", text)
    if "</think>" in text:  # opening tag was in the prompt template
        text = text.split("</think>", 1)[1]
    return text.strip()


def _as_call(obj: Any, tool_names: set[str]) -> ToolCall | None:
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("function") or obj.get("tool_name")
    if isinstance(name, dict):
        obj, name = name, name.get("name")
    args = obj.get("arguments", obj.get("args", obj.get("parameters", obj.get("input", {}))))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if not isinstance(name, str) or name not in tool_names or not isinstance(args, dict):
        return None
    return ToolCall(id=f"call_{uuid.uuid4().hex[:10]}", name=name, arguments=args)


def repair_tool_calls(content: str, tool_names: set[str]) -> list[ToolCall]:
    """Find tool calls a model wrote as text instead of using the tools API."""
    if not content or not tool_names:
        return []
    candidates: list[str] = TOOL_TAG_RE.findall(content) or FENCE_RE.findall(content)
    if not candidates:
        stripped = content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            candidates = [stripped]
    calls = []
    for c in candidates:
        obj = None
        doubled = c.startswith("{{")
        for attempt in (c, c[1:] if doubled else None, c[1:-1] if doubled and c.endswith("}}") else None,
                        re.sub(r",\s*([}\]])", r"\1", c)):
            if attempt is None:
                continue
            try:
                obj = json.loads(attempt)
                break
            except json.JSONDecodeError:
                continue
        if obj is None:
            continue
        for item in obj if isinstance(obj, list) else [obj]:
            call = _as_call(item, tool_names)
            if call:
                calls.append(call)
    return calls


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw or "{}")
            return v if isinstance(v, dict) else {"value": v}
        except json.JSONDecodeError:
            # Some small models emit single-quoted or trailing-comma JSON.
            fixed = re.sub(r",\s*([}\]])", r"\1", raw.replace("'", '"'))
            try:
                v = json.loads(fixed)
                return v if isinstance(v, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


class LLM:
    def __init__(self, base_url: str, api_key: str = "local", timeout: float = 600.0, transport: httpx.AsyncBaseTransport | None = None,
                 num_ctx: int = 16384):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport  # tests inject a mock server here
        self.num_ctx = num_ctx
        self._ollama: bool | None = None  # detected on first call

    @property
    def ollama_root(self) -> str:
        return self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url

    async def is_ollama(self) -> bool:
        """Ollama's OpenAI endpoint ignores context-size options, so talk to its native API when we can."""
        if self._ollama is None:
            try:
                async with self._client(3) as client:
                    r = await client.get(f"{self.ollama_root}/api/tags")
                    self._ollama = r.status_code == 200 and "models" in r.json()
            except Exception:
                self._ollama = False
        return self._ollama

    def reset(self) -> None:
        self._ollama = None

    async def _chat_ollama(self, model: str, messages: list[dict], tools: list[dict] | None, temperature: float) -> ChatResult:
        msgs = []
        for m in messages:
            mm = {"role": m["role"], "content": m.get("content") or ""}
            if m.get("tool_calls"):
                mm["tool_calls"] = [{"function": {"name": tc["function"]["name"], "arguments": _parse_args(tc["function"]["arguments"])}} for tc in m["tool_calls"]]
            msgs.append(mm)
        body: dict[str, Any] = {"model": model, "messages": msgs, "stream": False,
                                "options": {"temperature": temperature, "num_ctx": self.num_ctx}}
        if tools:
            body["tools"] = tools
        async with self._client(self.timeout) as client:
            r = await client.post(f"{self.ollama_root}/api/chat", json=body)
            if r.status_code == 400 and tools and "tool" in r.text.lower():
                body.pop("tools")
                body["messages"] = _inline_tools(msgs, tools)
                r = await client.post(f"{self.ollama_root}/api/chat", json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"Ollama returned {r.status_code}: {r.text[:300]}")
            data = r.json()
        msg = data.get("message") or {}
        tool_calls = [ToolCall(id=f"call_{uuid.uuid4().hex[:10]}", name=(tc.get("function") or {}).get("name", ""),
                               arguments=_parse_args((tc.get("function") or {}).get("arguments")))
                      for tc in (msg.get("tool_calls") or [])]
        content = strip_thinking(msg.get("content"))
        names = {t["function"]["name"] for t in tools or []}
        repaired = False
        if not tool_calls and names:
            tool_calls = repair_tool_calls(content, names)
            repaired = bool(tool_calls)
            if repaired:
                content = ""
        tokens = int(data.get("prompt_eval_count") or 0) + int(data.get("eval_count") or 0)
        return ChatResult(content=content, tool_calls=tool_calls, raw=msg, tokens=tokens, repaired=repaired)

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self.transport) if self.transport else httpx.AsyncClient(timeout=timeout)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.3) -> ChatResult:
        if model == "stub":
            return stub_chat(messages, tools or [])
        if await self.is_ollama():
            return await self._chat_ollama(model, messages, tools, temperature)
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature, "stream": False}
        if tools:
            body["tools"] = tools
        async with self._client(self.timeout) as client:
            r = await client.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers())
            if r.status_code == 400 and tools:
                # Model without native tool support: describe tools in the prompt and parse text.
                body.pop("tools")
                body["messages"] = _inline_tools(messages, tools)
                r = await client.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers())
            if r.status_code >= 400:
                raise RuntimeError(f"model server returned {r.status_code}: {r.text[:300]}")
            data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = [
            ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:10]}", name=(tc.get("function") or {}).get("name", ""),
                     arguments=_parse_args((tc.get("function") or {}).get("arguments")))
            for tc in (msg.get("tool_calls") or [])
        ]
        content = strip_thinking(msg.get("content"))
        repaired = False
        names = {t["function"]["name"] for t in tools or []}
        if not tool_calls and names:
            tool_calls = repair_tool_calls(content, names)
            repaired = bool(tool_calls)
            if repaired:
                content = ""
        usage = data.get("usage") or {}
        return ChatResult(content=content, tool_calls=tool_calls, raw=msg, tokens=int(usage.get("total_tokens") or 0), repaired=repaired)

    async def embed(self, model: str, texts: list[str]) -> list[list[float]] | None:
        if model in ("", "none", "stub"):
            return None
        try:
            async with self._client(60) as client:
                r = await client.post(f"{self.base_url}/embeddings", json={"model": model, "input": texts}, headers=self._headers())
                if r.status_code >= 400:
                    return None
                data = r.json().get("data") or []
                return [d["embedding"] for d in data] if len(data) == len(texts) else None
        except (httpx.HTTPError, ValueError, KeyError):
            return None

    async def list_models(self) -> list[str]:
        async with self._client(5) as client:
            r = await client.get(f"{self.base_url}/models", headers=self._headers())
            r.raise_for_status()
            return sorted(m["id"] for m in r.json().get("data", []))


def _inline_tools(messages: list[dict], tools: list[dict]) -> list[dict]:
    spec = "\n".join(f"- {t['function']['name']}: {t['function'].get('description','')} args={json.dumps(t['function'].get('parameters',{}).get('properties',{}))}" for t in tools)
    note = ("\n\nYou can use tools. To call one, reply with ONLY:\n<tool_call>{\"name\": \"tool_name\", \"arguments\": {...}}</tool_call>\n"
            f"Available tools:\n{spec}")
    out = [dict(m) for m in messages]
    if out and out[0]["role"] == "system":
        out[0]["content"] += note
    else:
        out.insert(0, {"role": "system", "content": note})
    # Servers without tool support also reject role=tool messages.
    for m in out:
        if m["role"] == "tool":
            m["role"] = "user"
            m["content"] = f"[tool result]\n{m['content']}"
            m.pop("tool_call_id", None)
        if m.get("tool_calls"):
            m["content"] = (m.get("content") or "") + "\n" + "\n".join(
                f"<tool_call>{json.dumps({'name': tc['function']['name'], 'arguments': tc['function']['arguments']})}</tool_call>" for tc in m["tool_calls"])
            m.pop("tool_calls")
    return out


# --- stub model ----------------------------------------------------------------------------------
# A scripted model so the whole app works with no model installed: for demos, tests and CI.

def stub_chat(messages: list[dict], tools: list[dict]) -> ChatResult:
    names = {t["function"]["name"] for t in tools}
    last = messages[-1]
    if last["role"] == "tool":
        result = last.get("content", "")
        if "denied" in result.lower() or "blocked" in result.lower():
            return ChatResult(content=f"Understood — I did not do that. ({result[:160]})")
        return ChatResult(content=f"✓ Done. Result: {result[:400]}")
    text = (last.get("content") or "")
    low = text.lower()

    def call(tool_name: str, **args) -> ChatResult:
        return ChatResult(content="", tool_calls=[ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=tool_name, arguments=args)])

    m = re.match(r"\[Reply from coworker ([^\]]+)\]\s*(.+)", text, re.S)
    if m:
        return ChatResult(content=f"{m.group(1)} is done: {m.group(2).strip()[:300]}")
    m = re.search(r"ask (\w+) to (.+)", text, re.I)
    if m and "message_coworker" in names:
        return call("message_coworker", to=m.group(1), task=m.group(2))
    m = re.search(r"remember (?:that )?(.+)", text, re.I)
    if m and "remember" in names:
        return call("remember", content=m.group(1).strip().rstrip("."))
    m = re.search(r"https?://\S+", text)
    if m and ("fetch" in low or "read" in low or "open" in low) and "web_fetch" in names:
        return call("web_fetch", url=m.group(0).rstrip(".,)"))
    m = re.search(r"email (\S+@\S+)", text, re.I)
    if m and "send_email" in names:
        return call("send_email", to=m.group(1).rstrip(".,"), subject="Hello from Agent Tillu", body=text)
    m = re.search(r"post (?:to )?(https?://\S+)", text, re.I)
    if m and "http_request" in names:
        return call("http_request", method="POST", url=m.group(1), body="hello")
    m = re.search(r"write (?:a )?file (\S+) (?:with|saying) (.+)", text, re.I)
    if m and "write_file" in names:
        return call("write_file", path=m.group(1), content=m.group(2))
    m = re.search(r"run `([^`]+)`", text)
    if m and "shell" in names:
        return call("shell", command=m.group(1))
    m = re.search(r"every (?:day|morning) .*?(?:to|:) (.+)", text, re.I)
    if m and "create_routine" in names:
        return call("create_routine", name="Daily task", cron="0 8 * * *", instructions=m.group(1))
    if "what do you remember" in low and "recall" in names:
        return call("recall", query=text)
    m = re.search(r"hire (?:a |an )?(.+?)(?: named| called) (\w+)", text, re.I)
    if m and "hire_coworker" in names:
        return call("hire_coworker", name=m.group(2), role=m.group(1), template="")
    if "which" in low and "?" in text and "ask_user" in names:
        return call("ask_user", question="Which option should I go with?", options=["Option A", "Option B"])
    return ChatResult(content=f"(stub model) I read: “{text[:200]}”. Connect a local model in Settings to get real answers.")
