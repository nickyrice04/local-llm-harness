"""MCP servers' HTTP surface: list, add, remove, reconnect."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from seymour import mcp

router = APIRouter(prefix="/api/mcp")


class ServerBody(BaseModel):
    """A server to add (or replace, by name)."""

    name: str
    command: str
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: str = ""


@router.get("")
async def overview():
    """Every configured server with its connection state and tools."""
    return {"servers": mcp.manager.overview()}


@router.get("/presets")
async def presets():
    """One-click servers, with whether their launcher (npx/uvx) exists here."""
    return {"presets": mcp.presets_overview()}


@router.post("")
async def add(body: ServerBody):
    """Configure + connect a server; its tools join the catalog live."""
    if not mcp.ServerConfig.valid_name(body.name):
        raise HTTPException(422, "name must be 1-32 characters of letters, digits, _ or -")
    if not body.command.strip():
        raise HTTPException(422, "command is required")
    server = await mcp.manager.add(mcp.ServerConfig(
        name=body.name, command=body.command.strip(), args=body.args, env=body.env, cwd=body.cwd))
    return {"connected": server.connected, "tools": [t["name"] for t in server.tools],
            "error": server.error}


@router.post("/{name}/reconnect")
async def reconnect(name: str):
    cfg = next((c for c in mcp.load_configs() if c.name == name), None)
    if cfg is None:
        raise HTTPException(404, "no such server")
    server = await mcp.manager.connect(cfg)
    return {"connected": server.connected, "tools": [t["name"] for t in server.tools],
            "error": server.error}


@router.delete("/{name}")
async def remove(name: str):
    await mcp.manager.remove(name)
    return {"removed": name}
