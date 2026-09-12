"""The MCP client against a fake stdio server: handshake, discovery,
registration under mcp__<server>__<tool>, a call, error rendering, and
clean unregistration. Model-free; the fake server is a few lines of
Python speaking newline-delimited JSON-RPC."""

import sys
import textwrap

import pytest

from seymour import mcp, tools

FAKE_SERVER = textwrap.dedent('''
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        m, i = msg.get("method"), msg.get("id")
        if m == "initialize":
            out = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                   "serverInfo": {"name": "fake", "version": "1"}}
        elif m == "tools/list":
            out = {"tools": [{"name": "echo", "description": "Echo text back",
                              "inputSchema": {"type": "object",
                                              "properties": {"text": {"type": "string", "description": "what to echo"},
                                                             "shout": {"type": "boolean"}},
                                              "required": ["text"]}},
                             {"name": "fail", "description": "Always errors", "inputSchema": {"type": "object", "properties": {}}}]}
        elif m == "tools/call":
            name = msg["params"]["name"]; args = msg["params"].get("arguments", {})
            if name == "echo":
                text = args.get("text", "")
                out = {"content": [{"type": "text", "text": text.upper() if args.get("shout") in (True, "true") else text}]}
            else:
                out = {"content": [{"type": "text", "text": "it broke"}], "isError": True}
        elif i is None:
            continue                      # a notification
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": "unknown method"}}) + "\\n"); sys.stdout.flush(); continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": i, "result": out}) + "\\n"); sys.stdout.flush()
''')


@pytest.fixture
def fake_server(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_SERVER)
    return mcp.ServerConfig(name="fake", command=sys.executable, args=[str(script)])


async def test_connect_registers_tools_and_calls_work(fake_server):
    manager = mcp.McpManager()
    server = await manager.connect(fake_server)
    try:
        assert server.connected and [t["name"] for t in server.tools] == ["echo", "fail"]
        assert "mcp__fake__echo" in tools.TOOLS and tools.TOOLS["mcp__fake__echo"].tier == "exec"
        echo = tools.TOOLS["mcp__fake__echo"]
        assert "text" in echo.args and "shout" in echo.optional
        assert await tools.execute("mcp__fake__echo", {"text": "hi", "shout": True}) == "HI"
        failed = await tools.execute("mcp__fake__fail", {})
        assert failed.startswith("Error:") and tools.is_error("mcp__fake__fail", failed)
        assert "mcp__fake__echo" in tools.render_catalog("full")
        assert "mcp__fake__echo" not in tools.render_catalog("read_only")   # exec tier is gated
    finally:
        await manager.shutdown()
        mcp._unregister("fake")
    assert "mcp__fake__echo" not in tools.TOOLS


async def test_bad_command_is_reported_not_raised():
    manager = mcp.McpManager()
    server = await manager.connect(mcp.ServerConfig(name="nope", command="/definitely/not/here"))
    assert not server.connected and "could not start" in server.error
    assert not any(n.startswith("mcp__nope__") for n in tools.TOOLS)


def test_public_names_are_safe():
    assert mcp._public_name("gh", "create issue!") == "mcp__gh__create_issue_"
    assert not mcp.ServerConfig.valid_name("bad name") and mcp.ServerConfig.valid_name("ok-1")
