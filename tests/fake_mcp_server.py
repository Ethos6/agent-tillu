"""A tiny MCP stdio server for tests."""
import json
import sys

TOOLS = [
    {"name": "get_weather", "description": "weather", "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "create_ticket", "description": "make a ticket", "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}}},
    {"name": "drop_table", "description": "drop", "inputSchema": {"type": "object", "properties": {}}, "annotations": {"destructiveHint": True}},
]
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m, rid = msg["method"], msg["id"]
    if m == "initialize":
        res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif m == "tools/list":
        res = {"tools": TOOLS}
    elif m == "tools/call":
        a = msg["params"].get("arguments", {})
        res = {"content": [{"type": "text", "text": f"{msg['params']['name']} ok {json.dumps(a)}"}]}
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}) + "\n")
    sys.stdout.flush()
