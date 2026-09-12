"""MCP client: mount external Model Context Protocol servers as tools.

How Claude Code and deepseek-harness get their long tail — GitHub,
databases, browsers, a company's own services — is MCP servers, each
exposing tools over JSON-RPC. dsh's `mcp-client` plugin is the template
followed here (see ACKNOWLEDGMENTS.md): one connection per configured
server, `tools/list` on connect, every tool registered under the
server-qualified name `mcp__<server>__<tool>`, calls forwarded with a
timeout, tools only (resources and prompts have no consumer here).

Transport is stdio (the server is a child process we spawn; JSON-RPC
messages are newline-delimited). It is written against the protocol
directly — ~200 lines — rather than the SDK, so there is nothing to
version-match and every byte on the wire is visible in this file.

Every MCP tool is tier "exec": Seymour does not know what a foreign tool
does, so the chat asks once per run before the first call, exactly as
for running a command. The unattended agent may call them freely, as it
may run commands — the person configured the server, that is the grant.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import json
import logging
import re
from dataclasses import dataclass, field

from seymour.db import get_state, set_state

logger = logging.getLogger(__name__)

# Per-call ceiling (dsh's default) and the connect/discovery ceiling.
# Where a server runs when no cwd is given: the Seymour install folder,
# so "scripts/x.py" in the Settings form means what a reader expects.
# (The app process itself may be started from anywhere — measured: its
# cwd was the PARENT folder, and a relative script path silently died.)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CALL_TIMEOUT_S = 60.0
CONNECT_TIMEOUT_S = 30.0
# Results are bounded like every other tool's (the registry caps again).
MAX_RESULT_CHARS = 8000
# app_state key holding the configured servers as JSON.
STATE_KEY = "mcp_servers"
# The protocol version we speak (the 2025-03 revision; servers negotiate).
PROTOCOL = "2025-03-26"


@dataclass
class ServerConfig:
    """One configured server: how to start it."""

    name: str                      # namespace: [A-Za-z0-9_-]{1,32}
    command: str                   # executable
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    # Where it was configured: "settings" (the UI, persisted in app_state)
    # or "file" (~/.seymour/mcp.json, the person's own editable config).
    source: str = "settings"

    @staticmethod
    def valid_name(name: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""))


def config_file() -> Path:
    """The person's own MCP config: ~/.seymour/mcp.json, in the shape
    Claude Code and dsh use, so a server definition copies straight in:

        {"mcpServers": {"github": {"command": "npx", "args": ["-y", "@x/server"],
                                   "env": {"TOKEN": "…"}, "cwd": ""}}}
    """
    from seymour.config import settings
    return settings.data_dir / "mcp.json"


def load_file_configs() -> tuple[list[ServerConfig], str]:
    """Servers from the config file; (configs, error text or "")."""
    path = config_file()
    if not path.exists():
        return [], ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [], f"{path.name}: {error}"
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return [], f"{path.name}: expected a top-level \"mcpServers\" object"
    out = []
    for name, row in servers.items():
        if not isinstance(row, dict) or not ServerConfig.valid_name(str(name)) or not str(row.get("command", "")).strip():
            continue
        out.append(ServerConfig(name=str(name), command=str(row["command"]),
                                args=[str(a) for a in row.get("args", [])],
                                env={str(k): str(v) for k, v in (row.get("env") or {}).items()},
                                cwd=str(row.get("cwd", "")), source="file"))
    return out, ""


def load_configs() -> list[ServerConfig]:
    """Every configured server: the UI's (app_state) plus the file's. On a
    name clash the file wins — it is the one the person edits by hand."""
    raw = get_state(STATE_KEY)
    out: list[ServerConfig] = []
    try:
        rows = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        rows = []
    for row in rows if isinstance(rows, list) else []:
        try:
            cfg = ServerConfig(name=str(row["name"]), command=str(row["command"]),
                               args=[str(a) for a in row.get("args", [])],
                               env={str(k): str(v) for k, v in (row.get("env") or {}).items()},
                               cwd=str(row.get("cwd", "")))
        except (KeyError, TypeError, AttributeError):
            continue
        if ServerConfig.valid_name(cfg.name):
            out.append(cfg)
    from_file, _error = load_file_configs()
    file_names = {c.name for c in from_file}
    return [c for c in out if c.name not in file_names] + from_file


def save_configs(configs: list[ServerConfig]) -> None:
    """Persist the UI-configured servers (file-sourced ones live in the file)."""
    set_state(STATE_KEY, json.dumps([{k: v for k, v in cfg.__dict__.items() if k != "source"}
                                     for cfg in configs if cfg.source != "file"]))


# ---- Presets: one-click servers -------------------------------------------
# Verified against the npm/PyPI registries on 2026-09-03 (see NOTES.md).
# Each says what it adds and what it costs in trust; the person still
# decides. {workspace} and {data} are filled at add time.
PRESETS: list[dict] = [
    {"name": "playwright",
     "command": "npx", "args": ["-y", "@playwright/mcp@latest", "--headless", "--isolated"],
     "needs": "npx",
     "adds": "a real browser: navigate, accessibility snapshot, click/type by ref, "
             "screenshots, PDF — for pages that only render with JavaScript",
     "trust": "everything it returns is untrusted web content; browser_evaluate runs "
              "JavaScript. First run may download a Chromium build."},
    {"name": "filesystem",
     "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "{workspace}"],
     "needs": "npx",
     "adds": "file tools scoped to the workspace folder (read, write, edit, search, tree)",
     "trust": "duplicates Seymour's own file tools and bypasses their secret-path "
              "denylist; keep it scoped to the workspace."},
    {"name": "fetch",
     "command": "uvx", "args": ["mcp-server-fetch"],
     "needs": "uvx",
     "adds": "an HTML-to-markdown fetch tool",
     "trust": "no private-address guard (Seymour's fetch_page has one); only if "
              "fetch_page cannot get a page."},
    {"name": "git",
     "command": "uvx", "args": ["mcp-server-git", "--repository", "{workspace}"],
     "needs": "uvx",
     "adds": "git status/diff/log/commit/branch tools for the workspace repository",
     "trust": "commit, reset and checkout change history; chat asks once per run."},
    {"name": "memory",
     "command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"],
     "env": {"MEMORY_FILE_PATH": "{data}/mcp-memory.jsonl"},
     "needs": "npx",
     "adds": "a knowledge-graph memory (entities, relations, observations)",
     "trust": "overlaps remember_fact; the graph is a plain JSONL file."},
]


def presets_overview() -> list[dict]:
    """The presets with their launcher's availability on this machine."""
    import shutil
    from seymour.config import settings
    out = []
    for preset in PRESETS:
        out.append({**preset,
                    "available": shutil.which(preset["needs"]) is not None,
                    "args": [a.replace("{workspace}", str(settings.workspace_dir))
                              .replace("{data}", str(settings.data_dir)) for a in preset["args"]],
                    "env": {k: v.replace("{data}", str(settings.data_dir))
                            for k, v in (preset.get("env") or {}).items()}})
    return out


class McpError(Exception):
    """A protocol-level failure the caller turns into readable text."""


class McpServer:
    """One live stdio connection: spawn, handshake, list, call, close."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.tools: list[dict] = []          # the server's advertised tools
        self.error: str = ""                 # last failure, for the UI
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()          # one writer at a time

    @property
    def connected(self) -> bool:
        return self.process is not None and self.process.returncode is None

    # ------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        """Spawn the server, run the initialize handshake, list tools."""
        import os
        env = {k: v for k, v in os.environ.items()
               if k in ("PATH", "HOME", "LANG", "TMPDIR", "USER")}
        env.update(self.config.env)          # only what the person configured
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.config.command, *self.config.args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env,
                cwd=self.config.cwd or str(PROJECT_ROOT))
        except (OSError, ValueError) as error:
            self.error = f"could not start {self.config.command}: {error}"
            raise McpError(self.error)
        self._reader = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._request("initialize", {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "seymour", "version": "0.2"},
            }), CONNECT_TIMEOUT_S)
            await self._notify("notifications/initialized", {})
            listed = await asyncio.wait_for(self._request("tools/list", {}), CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            self.error = "the server did not finish its handshake in time"
            await self.close()
            raise McpError(self.error)
        except McpError as error:
            # The reader saw the process die mid-handshake (a wrong path,
            # a missing interpreter): keep the reason where the UI shows
            # it — measured, the row said "down" with nothing else.
            self.error = str(error) or "the server exited during the handshake"
            await self.close()
            raise
        self.tools = [t for t in (listed or {}).get("tools", []) if isinstance(t, dict) and t.get("name")]
        self.error = ""
        logger.info("mcp %s: %d tools", self.config.name, len(self.tools))

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpError("connection closed"))
        self._pending.clear()

    # ------------------------------------------------------------- protocol
    async def _send(self, message: dict) -> None:
        assert self.process and self.process.stdin
        data = (json.dumps(message) + "\n").encode("utf-8")
        async with self._lock:
            self.process.stdin.write(data)
            await self.process.stdin.drain()

    async def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _read_loop(self) -> None:
        """Route responses to their waiting requests; ignore the rest."""
        assert self.process and self.process.stdout
        while True:
            line = await self.process.stdout.readline()
            if not line:
                break                                   # server exited
            try:
                message = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            future = self._pending.get(message.get("id")) if isinstance(message, dict) else None
            if future is None or future.done():
                continue                                # a notification, or late
            if "error" in message:
                err = message["error"] or {}
                future.set_exception(McpError(f"{err.get('message', 'error')} (code {err.get('code')})"))
            else:
                future.set_result(message.get("result") or {})
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(McpError("the server exited"))

    # ---------------------------------------------------------------- calls
    async def call(self, tool: str, arguments: dict) -> str:
        """tools/call, rendered as text (MCP content blocks joined)."""
        if not self.connected:
            raise McpError(f"{self.config.name} is not connected")
        result = await asyncio.wait_for(
            self._request("tools/call", {"name": tool, "arguments": arguments}),
            CALL_TIMEOUT_S)
        parts = []
        for block in result.get("content", []) if isinstance(result, dict) else []:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text":
                parts.append(str(block.get("text", "")))
            elif kind == "resource":
                res = block.get("resource") or {}
                parts.append(res.get("text") or f"[resource {res.get('uri', '')}]")
            elif kind:
                parts.append(f"[{kind} content omitted]")
        text = "\n".join(parts).strip() or json.dumps(result)[:MAX_RESULT_CHARS]
        if isinstance(result, dict) and result.get("isError"):
            text = "Error: " + text
        return text[:MAX_RESULT_CHARS]


# ------------------------------------------------------------------ manager
class McpManager:
    """All configured servers, and their tools' presence in the registry."""

    def __init__(self) -> None:
        self.servers: dict[str, McpServer] = {}

    async def startup(self) -> None:
        """Connect every configured server; a failure is reported, never fatal."""
        for cfg in load_configs():
            await self.connect(cfg)

    async def shutdown(self) -> None:
        for server in list(self.servers.values()):
            await server.close()
        self.servers.clear()

    async def connect(self, cfg: ServerConfig) -> McpServer:
        """(Re)connect one server and register its tools."""
        old = self.servers.pop(cfg.name, None)
        if old:
            await old.close()
            _unregister(cfg.name)
        server = McpServer(cfg)
        self.servers[cfg.name] = server
        try:
            await server.connect()
        except McpError as error:
            logger.warning("mcp %s failed: %s", cfg.name, error)
            return server
        _register(server)
        return server

    async def add(self, cfg: ServerConfig) -> McpServer:
        configs = [c for c in load_configs() if c.name != cfg.name] + [cfg]
        save_configs(configs)
        return await self.connect(cfg)

    async def remove(self, name: str) -> None:
        save_configs([c for c in load_configs() if c.name != name])
        server = self.servers.pop(name, None)
        if server:
            await server.close()
        _unregister(name)

    def overview(self) -> list[dict]:
        rows = []
        for cfg in load_configs():
            server = self.servers.get(cfg.name)
            specs = server.tools if server else []
            rows.append({"name": cfg.name, "command": cfg.command, "args": cfg.args,
                         "source": cfg.source,
                         "connected": bool(server and server.connected),
                         "tools": [t["name"] for t in specs],
                         # The REAL schemas from listTools: what each tool
                         # takes, as the server declared it (Settings shows them).
                         "tool_specs": [{"name": t["name"],
                                         "public": _public_name(cfg.name, t["name"]),
                                         "description": (t.get("description") or "")[:600],
                                         "schema": t.get("inputSchema") or {}} for t in specs],
                         "error": server.error if server else "not started"})
        return rows


def _public_name(server: str, tool: str) -> str:
    """dsh's shape: `mcp__<server>__<tool>`, normalized to safe characters."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool)[:40]
    return f"mcp__{server}__{safe}"


def _register(server: McpServer) -> None:
    """Put the server's tools in the ONE registry, tier exec."""
    from seymour import tools as registry
    for spec in server.tools:
        raw = spec["name"]
        props = ((spec.get("inputSchema") or {}).get("properties") or {})
        required = set((spec.get("inputSchema") or {}).get("required") or [])
        args = {k: str((v or {}).get("description") or (v or {}).get("type") or "value")
                for k, v in props.items()}

        async def func(_server=server, _raw=raw, **kwargs) -> str:
            try:
                return await _server.call(_raw, {k: v for k, v in kwargs.items() if v != ""})
            except asyncio.TimeoutError:
                return f"Error: {_raw} did not answer within {CALL_TIMEOUT_S:.0f}s"
            except McpError as error:
                return f"Error: {error}"

        name = _public_name(server.config.name, raw)
        registry.TOOLS[name] = registry.Tool(
            name=name,
            description=(str(spec.get("description") or "")[:300]
                         + f" (MCP tool from server {server.config.name})").strip(),
            args=args, optional=frozenset(k for k in args if k not in required),
            tier="exec", func=func,
            describe=lambda a, _n=raw, _s=server.config.name: f"use {_s}'s {_n} tool")


def _unregister(server_name: str) -> None:
    from seymour import tools as registry
    prefix = f"mcp__{server_name}__"
    for name in [n for n in registry.TOOLS if n.startswith(prefix)]:
        del registry.TOOLS[name]


manager = McpManager()
