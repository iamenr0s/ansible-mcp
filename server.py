#!/usr/bin/env python3
"""
Ansible MCP Server — HTTP/SSE transport.
Tools: run_playbook, run_molecule, manage_inventory,
       check_versions, run_shell
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route, Mount
import uvicorn

server    = Server("ansible-mcp")
WORKSPACE = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
WORKSPACE.mkdir(parents=True, exist_ok=True)


def run_cmd(cmd: list[str], cwd: Optional[str] = None, timeout: int = 300) -> dict:
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd or str(WORKSPACE),
            capture_output=True, text=True, timeout=timeout,
        )
        return {"stdout": r.stdout, "stderr": r.stderr, "returncode": r.returncode}
    except subprocess.TimeoutExpired as e:
        return {
            "stdout": e.stdout or "",
            "stderr": f"Timed out after {timeout}s\n{e.stderr or ''}".strip(),
            "returncode": -1,
        }
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1}


def safe_path(base: Path, user_input: str) -> Path:
    """Resolve a user-supplied path and assert it stays within base. Raises ValueError on escape."""
    resolved = (base / user_input).resolve()
    resolved.relative_to(base.resolve())  # raises ValueError if outside base
    return resolved


def fmt(r: dict) -> str:
    return (f"Exit code: {r['returncode']}\n\n"
            f"STDOUT:\n{r['stdout']}\n\nSTDERR:\n{r['stderr']}")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_playbook",
            description="Run an Ansible playbook",
            inputSchema={
                "type": "object",
                "required": ["playbook"],
                "properties": {
                    "playbook":   {"type": "string",  "description": "Path to playbook (relative to workspace)"},
                    "inventory":  {"type": "string",  "description": "Inventory file or host pattern"},
                    "extra_vars": {"type": "string",  "description": "Extra vars: key=val or JSON string"},
                    "tags":       {"type": "string",  "description": "Comma-separated tags"},
                    "check_mode": {"type": "boolean", "description": "Dry-run mode", "default": False},
                },
            },
        ),
        Tool(
            name="run_molecule",
            description="Run Molecule test scenarios against an Ansible role",
            inputSchema={
                "type": "object",
                "properties": {
                    "action":    {"type": "string", "enum": ["test","converge","verify","destroy","create","lint"], "default": "test"},
                    "scenario":  {"type": "string", "description": "Scenario name", "default": "default"},
                    "role_path": {"type": "string", "description": "Role path relative to workspace"},
                    "driver":    {"type": "string", "enum": ["podman","libvirt","qemu","default"], "description": "Driver override"},
                },
            },
        ),
        Tool(
            name="manage_inventory",
            description="Create, read, list, or delete inventory files under workspace/inventory/",
            inputSchema={
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action":   {"type": "string", "enum": ["read","write","list","delete"]},
                    "filename": {"type": "string", "description": "Inventory filename (required for read/write/delete)"},
                    "content":  {"type": "string", "description": "INI or YAML content (required for write)"},
                },
            },
        ),
        Tool(
            name="check_versions",
            description="Show installed versions of ansible, molecule, testinfra and related packages",
            inputSchema={
                "type": "object",
                "properties": {
                    "packages": {"type": "array", "items": {"type": "string"},
                                 "description": "Package list (omit for all managed packages)"},
                },
            },
        ),
        Tool(
            name="run_shell",
            description="Run an arbitrary shell command inside the container workspace",
            inputSchema={
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {"type": "string"},
                    "cwd":     {"type": "string", "description": "Working dir relative to workspace"},
                    "timeout": {"type": "integer", "default": 60},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    match name:
        case "run_playbook":      return await _run_playbook(arguments)
        case "run_molecule":      return await _run_molecule(arguments)
        case "manage_inventory":  return await _manage_inventory(arguments)
        case "check_versions":    return await _check_versions(arguments)
        case "run_shell":         return await _run_shell(arguments)
        case _:                   return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _run_playbook(a: dict) -> list[TextContent]:
    try:
        playbook = safe_path(WORKSPACE, a["playbook"])
    except ValueError:
        return [TextContent(type="text", text="Error: playbook path outside workspace")]
    cmd = ["ansible-playbook", str(playbook)]
    if a.get("inventory"):  cmd += ["-i", a["inventory"]]
    if a.get("extra_vars"): cmd += ["--extra-vars", a["extra_vars"]]
    if a.get("tags"):       cmd += ["--tags", a["tags"]]
    if a.get("check_mode"): cmd.append("--check")
    return [TextContent(type="text", text=fmt(run_cmd(cmd)))]


async def _run_molecule(a: dict) -> list[TextContent]:
    cmd = ["molecule", a.get("action", "test"), "-s", a.get("scenario", "default")]
    if a.get("driver"): cmd += ["--driver-name", a["driver"]]
    if a.get("role_path"):
        try:
            cwd = str(safe_path(WORKSPACE, a["role_path"]))
        except ValueError:
            return [TextContent(type="text", text="Error: role_path outside workspace")]
    else:
        cwd = str(WORKSPACE)
    return [TextContent(type="text", text=fmt(run_cmd(cmd, cwd=cwd, timeout=600)))]


async def _manage_inventory(a: dict) -> list[TextContent]:
    inv_dir = WORKSPACE / "inventory"
    inv_dir.mkdir(parents=True, exist_ok=True)
    action = a["action"]
    if action == "list":
        names = [f.name for f in inv_dir.iterdir()]
        return [TextContent(type="text", text=f"Files: {names or '(empty)'}")]
    fname = a.get("filename")
    if not fname:
        return [TextContent(type="text", text="Error: 'filename' required.")]
    try:
        path = safe_path(inv_dir, fname)
    except ValueError:
        return [TextContent(type="text", text="Error: filename outside inventory directory")]
    if action == "read":
        return [TextContent(type="text", text=path.read_text() if path.exists() else f"Not found: {fname}")]
    if action == "write":
        path.write_text(a.get("content", ""))
        return [TextContent(type="text", text=f"Written: {path}")]
    if action == "delete":
        path.unlink(missing_ok=True)
        return [TextContent(type="text", text=f"Deleted: {fname}")]
    return [TextContent(type="text", text=f"Unknown action: {action}")]


async def _check_versions(a: dict) -> list[TextContent]:
    pkgs = a.get("packages") or [
        "ansible", "ansible-core", "molecule", "molecule-libvirt",
        "molecule-podman", "molecule-qemu", "testinfra", "ansible-lint",
    ]
    r = run_cmd(["pip", "show"] + pkgs)
    return [TextContent(type="text", text=r["stdout"] or r["stderr"])]


async def _run_shell(a: dict) -> list[TextContent]:
    if a.get("cwd"):
        try:
            cwd = str(safe_path(WORKSPACE, a["cwd"]))
        except ValueError:
            return [TextContent(type="text", text="Error: cwd outside workspace")]
    else:
        cwd = str(WORKSPACE)
    r   = run_cmd(["bash", "-c", a["command"]], cwd=cwd, timeout=a.get("timeout", 60))
    return [TextContent(type="text", text=fmt(r))]


def create_app():
    sse = SseServerTransport("/messages/")
    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as (r_stream, w_stream):
            await server.run(r_stream, w_stream, server.create_initialization_options())
    return Starlette(routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ])


if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
    )
