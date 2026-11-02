import json

import httpx
import pytest

from tillu.llm import LLM, repair_tool_calls, strip_thinking

def test_repair_and_think():
    names = {"web_fetch", "remember"}
    c = repair_tool_calls('<tool_call>{"name": "web_fetch", "arguments": {"url": "https://a.b"}}</tool_call>', names)
    assert c and c[0].name == "web_fetch" and c[0].arguments["url"] == "https://a.b"
    c = repair_tool_calls('Sure!\n```json\n{"tool": "remember", "args": {"content": "x"}}\n```', names)
    assert c and c[0].name == "remember"
    assert repair_tool_calls('{"name": "rm_everything", "arguments": {}}', names) == []
    assert repair_tool_calls("just text", names) == []
    c = repair_tool_calls('<tool_call>\n{{"name": "remember", "arguments": {"content": "y"}}}\n</tool_call>', names)
    assert c and c[0].arguments == {"content": "y"}
    real = '<tool_call>\n{{"name": "web_fetch", "arguments": {"url": "https://a.b"}}\n</tool_call>'  # seen from Qwen2.5 GGUF
    c = repair_tool_calls(real, names)
    assert c and c[0].arguments == {"url": "https://a.b"}
    assert strip_thinking("<think>hmm</think>Answer") == "Answer"
    assert strip_thinking("reasoning...</think>\nAnswer") == "Answer"


def _tool(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}}


@pytest.mark.asyncio
async def test_openai_compatible_tool_calls():
    seen = {}

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen["body"] = body
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "<think>plan</think>",
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "web_fetch", "arguments": "{\"url\": \"https://a.b\"}"}}]}}],
            "usage": {"total_tokens": 42}})
    llm = LLM("http://ollama.local/v1", transport=httpx.MockTransport(handler))
    r = await llm.chat("qwen3:8b", [{"role": "user", "content": "hi"}], [_tool("web_fetch")])
    assert seen["body"]["model"] == "qwen3:8b" and seen["body"]["tools"]
    assert r.tool_calls[0].name == "web_fetch" and r.tool_calls[0].arguments == {"url": "https://a.b"}
    assert r.content == "" and r.tokens == 42


@pytest.mark.asyncio
async def test_fallback_when_server_rejects_tools():
    calls = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append(body)
        if "tools" in body:
            return httpx.Response(400, json={"error": "model does not support tools"})
        assert "<tool_call>" in body["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": '<tool_call>{"name": "web_fetch", "arguments": {"url": "https://x.y"}}</tool_call>'}}]})
    llm = LLM("http://x/v1", transport=httpx.MockTransport(handler))
    r = await llm.chat("tiny", [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}], [_tool("web_fetch")])
    assert len(calls) == 2 and r.repaired and r.tool_calls[0].arguments["url"] == "https://x.y"


@pytest.mark.asyncio
async def test_embed_and_models():
    def handler(req: httpx.Request):
        if req.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
        return httpx.Response(200, json={"data": [{"id": "qwen3:8b"}, {"id": "llama3.1:8b"}]})
    llm = LLM("http://x/v1", transport=httpx.MockTransport(handler))
    assert await llm.embed("nomic-embed-text", ["a"]) == [[0.1, 0.2]]
    assert await llm.list_models() == ["llama3.1:8b", "qwen3:8b"]


@pytest.mark.asyncio
async def test_ollama_native_api_is_used_with_num_ctx():
    seen = {}

    def handler(req: httpx.Request):
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        if req.url.path == "/api/chat":
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "<think>x</think>",
                                                         "tool_calls": [{"function": {"name": "web_fetch", "arguments": {"url": "https://q.r"}}}]},
                                             "prompt_eval_count": 100, "eval_count": 20})
        return httpx.Response(404)
    llm = LLM("http://127.0.0.1:11434/v1", transport=httpx.MockTransport(handler), num_ctx=32768)
    r = await llm.chat("qwen3:8b", [{"role": "user", "content": "hi"}], [_tool("web_fetch")])
    assert seen["body"]["options"]["num_ctx"] == 32768 and seen["body"]["tools"]
    assert r.tool_calls[0].arguments == {"url": "https://q.r"} and r.tokens == 120 and r.content == ""
