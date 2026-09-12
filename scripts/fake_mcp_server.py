#!/usr/bin/env python3
"""A tiny MCP server over stdio, for trying Seymour's MCP client without
installing anything: two tools, `echo` and `now`.

Configure it in Settings → MCP servers as
    name: demo   command: <repo>/.venv/bin/python   args: scripts/fake_mcp_server.py
and the chat gains mcp__demo__echo and mcp__demo__now.
"""

import datetime
import json
import sys

TOOLS = [
    {"name": "echo", "description": "Echo text back, optionally shouted.",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string", "description": "what to echo"},
                                    "shout": {"type": "boolean", "description": "uppercase it"}},
                     "required": ["text"]}},
    {"name": "now", "description": "The current local date and time.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def reply(message_id, result=None, error=None) -> None:
    body = {"jsonrpc": "2.0", "id": message_id}
    body["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(body) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    method, message_id = message.get("method"), message.get("id")
    if method == "initialize":
        reply(message_id, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "seymour-demo", "version": "1"}})
    elif method == "tools/list":
        reply(message_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = message.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            text = str(args.get("text", ""))
            if str(args.get("shout", "")).lower() in ("true", "1", "yes"):
                text = text.upper()
            reply(message_id, {"content": [{"type": "text", "text": text}]})
        elif name == "now":
            reply(message_id, {"content": [{"type": "text",
                                            "text": datetime.datetime.now().isoformat(timespec="seconds")}]})
        else:
            reply(message_id, {"content": [{"type": "text", "text": f"unknown tool {name}"}],
                               "isError": True})
    elif message_id is not None:
        reply(message_id, error={"code": -32601, "message": f"unknown method {method}"})
