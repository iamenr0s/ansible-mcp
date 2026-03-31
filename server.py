#!/usr/bin/env python3
"""
Ansible MCP Server — HTTP/SSE transport.
42 tools across inventory, playbooks, projects, vault, galaxy,
diagnostics, security analysis, and infrastructure monitoring.
"""

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import yaml

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Route, Mount
import uvicorn

server    = Server("ansible-mcp")
WORKSPACE = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
WORKSPACE.mkdir(parents=True, exist_ok=True)
PROJECTS_FILE = WORKSPACE / "projects.json"


# ─── Core Helpers ─────────────────────────────────────────────────────────────

def run_cmd(cmd: list, cwd: Optional[str] = None, timeout: int = 300,
            env: Optional[dict] = None) -> dict:
    merged_env = {**os.environ, **(env or {})}
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd or str(WORKSPACE),
            capture_output=True, text=True, timeout=timeout,
            env=merged_env,
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
    """Resolve user-supplied path and assert it stays within base. Raises ValueError on escape."""
    resolved = (base / user_input).resolve()
    resolved.relative_to(base.resolve())
    return resolved


def fmt(r: dict) -> str:
    return (f"Exit code: {r['returncode']}\n\nSTDOUT:\n{r['stdout']}\n\nSTDERR:\n{r['stderr']}")


def ok(data: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, indent=2))]


def err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"ok": False, "error": msg}))]


# ─── Project Config ────────────────────────────────────────────────────────────

def load_projects() -> dict:
    if PROJECTS_FILE.exists():
        try:
            return json.loads(PROJECTS_FILE.read_text())
        except Exception:
            pass
    return {"projects": {}, "default": None}


def save_projects(data: dict) -> None:
    PROJECTS_FILE.write_text(json.dumps(data, indent=2))


def compose_env(project: dict) -> dict:
    env: dict = {}
    if project.get("roles_path"):
        env["ANSIBLE_ROLES_PATH"] = project["roles_path"]
    if project.get("collections_path"):
        env["ANSIBLE_COLLECTIONS_PATHS"] = project["collections_path"]
    if project.get("ansible_config"):
        env["ANSIBLE_CONFIG"] = project["ansible_config"]
    env.update(project.get("env") or {})
    return env


def _resolve_inv_env(a: dict) -> tuple[Optional[str], dict]:
    """Return (inventory_path_or_None, env_dict) from tool args + optional project."""
    projects = load_projects()
    env: dict = {}
    inv: Optional[str] = a.get("inventory")
    project_name = a.get("project") or projects.get("default")
    if project_name and project_name in projects.get("projects", {}):
        proj = projects["projects"][project_name]
        env = compose_env(proj)
        if not inv and proj.get("inventory"):
            inv = proj["inventory"]
    return inv, env


def _project_cwd(a: dict) -> Optional[str]:
    """Return project root cwd if a project is referenced."""
    projects = load_projects()
    project_name = a.get("project") or projects.get("default")
    if project_name and project_name in projects.get("projects", {}):
        proj = projects["projects"][project_name]
        if proj.get("root"):
            try:
                return str(safe_path(WORKSPACE, proj["root"]))
            except ValueError:
                pass
    return None


# ─── Output Parsers ───────────────────────────────────────────────────────────

def parse_play_recap(output: str) -> dict:
    recap: dict = {}
    pattern = re.compile(
        r"(\S+)\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)"
    )
    for m in pattern.finditer(output):
        recap[m.group(1)] = {
            "ok": int(m.group(2)),
            "changed": int(m.group(3)),
            "unreachable": int(m.group(4)),
            "failed": int(m.group(5)),
        }
    return recap


# ─── Vault Helpers ────────────────────────────────────────────────────────────

@contextmanager
def vault_password_file(password: str):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".vault_pass", delete=False) as f:
        f.write(password)
        fname = f.name
    try:
        yield fname
    finally:
        Path(fname).unlink(missing_ok=True)


# ─── Temp Playbook Helper ─────────────────────────────────────────────────────

@contextmanager
def temp_playbook(content: list):
    """Write a playbook list to a temp file and yield its path."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yml", dir=str(WORKSPACE), delete=False
    ) as f:
        yaml.dump(content, f, default_flow_style=False)
        fname = f.name
    try:
        yield fname
    finally:
        Path(fname).unlink(missing_ok=True)


# ─── Tool Definitions ─────────────────────────────────────────────────────────

TOOLS: list[Tool] = [

    # ── Legacy / Backwards-Compat ─────────────────────────────────────────────
    Tool(name="run_playbook",
         description="Run an Ansible playbook (legacy — prefer ansible_playbook)",
         inputSchema={
             "type": "object", "required": ["playbook"],
             "properties": {
                 "playbook":   {"type": "string",  "description": "Path relative to workspace"},
                 "inventory":  {"type": "string"},
                 "extra_vars": {"type": "string",  "description": "key=val or JSON string"},
                 "tags":       {"type": "string"},
                 "check_mode": {"type": "boolean", "default": False},
             },
         }),
    Tool(name="run_molecule",
         description="Run Molecule test scenarios against an Ansible role",
         inputSchema={
             "type": "object",
             "properties": {
                 "action":    {"type": "string", "enum": ["test","converge","verify","destroy","create","lint"], "default": "test"},
                 "scenario":  {"type": "string", "default": "default"},
                 "role_path": {"type": "string"},
                 "driver":    {"type": "string", "enum": ["podman","libvirt","qemu","default"]},
             },
         }),
    Tool(name="manage_inventory",
         description="Create, read, list, or delete inventory files under workspace/inventory/",
         inputSchema={
             "type": "object", "required": ["action"],
             "properties": {
                 "action":   {"type": "string", "enum": ["read","write","list","delete"]},
                 "filename": {"type": "string"},
                 "content":  {"type": "string"},
             },
         }),
    Tool(name="check_versions",
         description="Show installed versions of ansible, molecule, testinfra, and related packages",
         inputSchema={
             "type": "object",
             "properties": {
                 "packages": {"type": "array", "items": {"type": "string"}},
             },
         }),
    Tool(name="run_shell",
         description="Run an arbitrary shell command inside the container workspace",
         inputSchema={
             "type": "object", "required": ["command"],
             "properties": {
                 "command": {"type": "string"},
                 "cwd":     {"type": "string"},
                 "timeout": {"type": "integer", "default": 60},
             },
         }),

    # ── Inventory ─────────────────────────────────────────────────────────────
    Tool(name="ansible_inventory",
         description="List all hosts and groups from an inventory file as structured JSON",
         inputSchema={
             "type": "object",
             "properties": {
                 "inventory":    {"type": "string"},
                 "include_vars": {"type": "boolean", "default": False},
                 "project":      {"type": "string"},
             },
         }),
    Tool(name="inventory_graph",
         description="Show the hierarchical group structure of an inventory",
         inputSchema={
             "type": "object",
             "properties": {
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="inventory_find_host",
         description="Find a specific host: its group memberships and merged variables",
         inputSchema={
             "type": "object", "required": ["host"],
             "properties": {
                 "host":      {"type": "string"},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="inventory_diff",
         description="Compare two inventory files and report added/removed hosts and groups",
         inputSchema={
             "type": "object", "required": ["inventory_a", "inventory_b"],
             "properties": {
                 "inventory_a": {"type": "string"},
                 "inventory_b": {"type": "string"},
             },
         }),
    Tool(name="inventory_parse",
         description="Parse an inventory with ansible.cfg-aware environment and return structured data",
         inputSchema={
             "type": "object",
             "properties": {
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),

    # ── Playbook Execution ────────────────────────────────────────────────────
    Tool(name="ansible_playbook",
         description="Run an Ansible playbook with full option support (verbosity, limit, diff, skip_tags)",
         inputSchema={
             "type": "object", "required": ["playbook"],
             "properties": {
                 "playbook":   {"type": "string"},
                 "inventory":  {"type": "string"},
                 "extra_vars": {"type": "object", "description": "Extra variables as JSON object"},
                 "tags":       {"type": "string"},
                 "skip_tags":  {"type": "string"},
                 "limit":      {"type": "string"},
                 "check_mode": {"type": "boolean", "default": False},
                 "diff":       {"type": "boolean", "default": False},
                 "verbosity":  {"type": "integer", "minimum": 0, "maximum": 4, "default": 0},
                 "project":    {"type": "string"},
             },
         }),
    Tool(name="ansible_task",
         description="Run an ad-hoc Ansible module against one or more hosts",
         inputSchema={
             "type": "object", "required": ["hosts", "module"],
             "properties": {
                 "hosts":      {"type": "string"},
                 "module":     {"type": "string", "description": "Module name (ping, shell, setup, copy, etc.)"},
                 "args":       {"type": "string", "description": "Module arguments string"},
                 "inventory":  {"type": "string"},
                 "become":     {"type": "boolean", "default": False},
                 "connection": {"type": "string", "enum": ["ssh","local","paramiko"], "default": "ssh"},
                 "project":    {"type": "string"},
             },
         }),
    Tool(name="ansible_role",
         description="Execute an Ansible role against hosts via a temporary playbook",
         inputSchema={
             "type": "object", "required": ["hosts", "role"],
             "properties": {
                 "hosts":      {"type": "string"},
                 "role":       {"type": "string"},
                 "inventory":  {"type": "string"},
                 "extra_vars": {"type": "object"},
                 "become":     {"type": "boolean", "default": False},
                 "project":    {"type": "string"},
             },
         }),
    Tool(name="validate_playbook",
         description="Syntax-check a playbook without executing it",
         inputSchema={
             "type": "object", "required": ["playbook"],
             "properties": {
                 "playbook":  {"type": "string"},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),

    # ── Authoring ─────────────────────────────────────────────────────────────
    Tool(name="create_playbook",
         description="Write a new YAML playbook file to the workspace",
         inputSchema={
             "type": "object", "required": ["filename", "content"],
             "properties": {
                 "filename": {"type": "string", "description": "Destination path relative to workspace"},
                 "content":  {"type": "string", "description": "YAML content"},
             },
         }),
    Tool(name="validate_yaml",
         description="Validate a YAML file and report syntax errors with line/column information",
         inputSchema={
             "type": "object", "required": ["filename"],
             "properties": {
                 "filename": {"type": "string"},
             },
         }),

    # ── Testing ───────────────────────────────────────────────────────────────
    Tool(name="ansible_test_idempotence",
         description="Run a playbook twice and verify no changes occur on the second run",
         inputSchema={
             "type": "object", "required": ["playbook"],
             "properties": {
                 "playbook":   {"type": "string"},
                 "inventory":  {"type": "string"},
                 "extra_vars": {"type": "object"},
                 "project":    {"type": "string"},
             },
         }),

    # ── Project Management ────────────────────────────────────────────────────
    Tool(name="register_project",
         description="Register an Ansible project with inventory, roles, and environment settings",
         inputSchema={
             "type": "object", "required": ["name", "root"],
             "properties": {
                 "name":             {"type": "string"},
                 "root":             {"type": "string", "description": "Project root relative to workspace"},
                 "inventory":        {"type": "string"},
                 "roles_path":       {"type": "string"},
                 "collections_path": {"type": "string"},
                 "ansible_config":   {"type": "string"},
                 "env":              {"type": "object"},
                 "set_default":      {"type": "boolean", "default": False},
             },
         }),
    Tool(name="list_projects",
         description="List all registered Ansible projects and the current default",
         inputSchema={"type": "object", "properties": {}}),
    Tool(name="project_playbooks",
         description="Discover YAML playbook files within a registered project root",
         inputSchema={
             "type": "object", "required": ["project"],
             "properties": {
                 "project": {"type": "string"},
             },
         }),
    Tool(name="project_run_playbook",
         description="Run a playbook using a registered project's stored configuration",
         inputSchema={
             "type": "object", "required": ["project", "playbook"],
             "properties": {
                 "project":    {"type": "string"},
                 "playbook":   {"type": "string"},
                 "extra_vars": {"type": "object"},
                 "tags":       {"type": "string"},
                 "limit":      {"type": "string"},
                 "check_mode": {"type": "boolean", "default": False},
             },
         }),
    Tool(name="project_bootstrap",
         description="Bootstrap a project: install Galaxy dependencies and report Ansible environment",
         inputSchema={
             "type": "object", "required": ["project"],
             "properties": {
                 "project":      {"type": "string"},
                 "requirements": {"type": "string", "default": "requirements.yml"},
             },
         }),

    # ── Vault ─────────────────────────────────────────────────────────────────
    Tool(name="vault_encrypt",
         description="Encrypt a file with ansible-vault",
         inputSchema={
             "type": "object", "required": ["filename", "password"],
             "properties": {
                 "filename": {"type": "string"},
                 "password": {"type": "string"},
             },
         }),
    Tool(name="vault_decrypt",
         description="Decrypt an ansible-vault encrypted file",
         inputSchema={
             "type": "object", "required": ["filename", "password"],
             "properties": {
                 "filename": {"type": "string"},
                 "password": {"type": "string"},
             },
         }),
    Tool(name="vault_view",
         description="View ansible-vault encrypted file contents without decrypting to disk",
         inputSchema={
             "type": "object", "required": ["filename", "password"],
             "properties": {
                 "filename": {"type": "string"},
                 "password": {"type": "string"},
             },
         }),
    Tool(name="vault_rekey",
         description="Change the encryption password on an ansible-vault encrypted file",
         inputSchema={
             "type": "object", "required": ["filename", "old_password", "new_password"],
             "properties": {
                 "filename":     {"type": "string"},
                 "old_password": {"type": "string"},
                 "new_password": {"type": "string"},
             },
         }),

    # ── Galaxy ────────────────────────────────────────────────────────────────
    Tool(name="galaxy_install",
         description="Install Ansible roles and/or collections from a requirements.yml file",
         inputSchema={
             "type": "object", "required": ["requirements"],
             "properties": {
                 "requirements": {"type": "string"},
                 "force":        {"type": "boolean", "default": False},
                 "project":      {"type": "string"},
             },
         }),
    Tool(name="galaxy_lock",
         description="Capture currently installed role and collection versions to a lock file",
         inputSchema={
             "type": "object",
             "properties": {
                 "output":  {"type": "string", "default": "galaxy-lock.yml"},
                 "project": {"type": "string"},
             },
         }),

    # ── Diagnostics ───────────────────────────────────────────────────────────
    Tool(name="ansible_gather_facts",
         description="Collect system facts from hosts using the Ansible setup module",
         inputSchema={
             "type": "object", "required": ["hosts"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "inventory": {"type": "string"},
                 "filter":    {"type": "string", "description": "Fact filter pattern e.g. ansible_os*"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="ansible_ping",
         description="Test Ansible connectivity to hosts using the ping module",
         inputSchema={
             "type": "object", "required": ["hosts"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="ansible_remote_command",
         description="Execute a shell command on remote hosts and return structured output",
         inputSchema={
             "type": "object", "required": ["hosts", "command"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "command":   {"type": "string"},
                 "inventory": {"type": "string"},
                 "become":    {"type": "boolean", "default": False},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="ansible_fetch_logs",
         description="Fetch and analyse log files from remote hosts",
         inputSchema={
             "type": "object", "required": ["hosts", "log_path"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "log_path":  {"type": "string"},
                 "lines":     {"type": "integer", "default": 100},
                 "pattern":   {"type": "string"},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="ansible_service_manager",
         description="Manage systemd services on remote hosts (start/stop/restart/status/enable/disable)",
         inputSchema={
             "type": "object", "required": ["hosts", "service", "action"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "service":   {"type": "string"},
                 "action":    {"type": "string", "enum": ["start","stop","restart","status","enable","disable"]},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="ansible_diagnose_host",
         description="Perform a comprehensive health assessment of hosts (CPU, memory, disk, network, services)",
         inputSchema={
             "type": "object", "required": ["hosts"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="ansible_health_monitor",
         description="Collect system metrics at intervals and report trends",
         inputSchema={
             "type": "object", "required": ["hosts"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "interval":  {"type": "integer", "default": 10},
                 "samples":   {"type": "integer", "default": 3},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),

    # ── Security & Analysis ───────────────────────────────────────────────────
    Tool(name="ansible_performance_baseline",
         description="Run performance benchmarks (CPU, memory, disk I/O) on hosts",
         inputSchema={
             "type": "object", "required": ["hosts"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="ansible_capture_baseline",
         description="Snapshot current system state (processes, network, configs) to a JSON file for later comparison",
         inputSchema={
             "type": "object", "required": ["hosts"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "output":    {"type": "string", "default": "baseline.json"},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="ansible_compare_states",
         description="Compare current system state against a captured baseline to detect configuration drift",
         inputSchema={
             "type": "object", "required": ["hosts", "baseline"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "baseline":  {"type": "string"},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="ansible_auto_heal",
         description="Diagnose and optionally fix common issues (high_cpu, high_memory, disk_full, service_failed, network_unreachable)",
         inputSchema={
             "type": "object", "required": ["hosts", "symptom"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "symptom":   {"type": "string", "enum": ["high_cpu","high_memory","disk_full","service_failed","network_unreachable"]},
                 "service":   {"type": "string", "description": "Required for service_failed"},
                 "dry_run":   {"type": "boolean", "default": True},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="ansible_network_matrix",
         description="Test port connectivity between hosts and build a connectivity matrix",
         inputSchema={
             "type": "object", "required": ["hosts"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "ports":     {"type": "array", "items": {"type": "integer"}, "default": [22, 80, 443]},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="ansible_security_audit",
         description="Security audit: open ports, SSH config, world-writable files, failed login attempts",
         inputSchema={
             "type": "object", "required": ["hosts"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
    Tool(name="ansible_log_hunter",
         description="Search multiple log files for a pattern and correlate events within a time window",
         inputSchema={
             "type": "object", "required": ["hosts", "pattern"],
             "properties": {
                 "hosts":     {"type": "string"},
                 "pattern":   {"type": "string"},
                 "log_paths": {"type": "array", "items": {"type": "string"},
                               "default": ["/var/log/syslog", "/var/log/messages", "/var/log/auth.log"]},
                 "window":    {"type": "integer", "default": 60, "description": "Correlation window in seconds"},
                 "inventory": {"type": "string"},
                 "project":   {"type": "string"},
             },
         }),
]


# ─── Tool Dispatch ────────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    dispatch = {
        # Legacy
        "run_playbook":              _run_playbook,
        "run_molecule":              _run_molecule,
        "manage_inventory":          _manage_inventory,
        "check_versions":            _check_versions,
        "run_shell":                 _run_shell,
        # Inventory
        "ansible_inventory":         _ansible_inventory,
        "inventory_graph":           _inventory_graph,
        "inventory_find_host":       _inventory_find_host,
        "inventory_diff":            _inventory_diff,
        "inventory_parse":           _inventory_parse,
        # Playbook execution
        "ansible_playbook":          _ansible_playbook,
        "ansible_task":              _ansible_task,
        "ansible_role":              _ansible_role,
        "validate_playbook":         _validate_playbook,
        # Authoring
        "create_playbook":           _create_playbook,
        "validate_yaml":             _validate_yaml,
        # Testing
        "ansible_test_idempotence":  _ansible_test_idempotence,
        # Projects
        "register_project":          _register_project,
        "list_projects":             _list_projects,
        "project_playbooks":         _project_playbooks,
        "project_run_playbook":      _project_run_playbook,
        "project_bootstrap":         _project_bootstrap,
        # Vault
        "vault_encrypt":             _vault_encrypt,
        "vault_decrypt":             _vault_decrypt,
        "vault_view":                _vault_view,
        "vault_rekey":               _vault_rekey,
        # Galaxy
        "galaxy_install":            _galaxy_install,
        "galaxy_lock":               _galaxy_lock,
        # Diagnostics
        "ansible_gather_facts":      _ansible_gather_facts,
        "ansible_ping":              _ansible_ping,
        "ansible_remote_command":    _ansible_remote_command,
        "ansible_fetch_logs":        _ansible_fetch_logs,
        "ansible_service_manager":   _ansible_service_manager,
        "ansible_diagnose_host":     _ansible_diagnose_host,
        "ansible_health_monitor":    _ansible_health_monitor,
        # Security & Analysis
        "ansible_performance_baseline": _ansible_performance_baseline,
        "ansible_capture_baseline":  _ansible_capture_baseline,
        "ansible_compare_states":    _ansible_compare_states,
        "ansible_auto_heal":         _ansible_auto_heal,
        "ansible_network_matrix":    _ansible_network_matrix,
        "ansible_security_audit":    _ansible_security_audit,
        "ansible_log_hunter":        _ansible_log_hunter,
    }
    handler = dispatch.get(name)
    if handler is None:
        return err(f"Unknown tool: {name}")
    return await handler(arguments)


# ─── Legacy Tool Implementations ─────────────────────────────────────────────

async def _run_playbook(a: dict) -> list[TextContent]:
    try:
        playbook = safe_path(WORKSPACE, a["playbook"])
    except ValueError:
        return err("playbook path outside workspace")
    cmd = ["ansible-playbook", str(playbook)]
    if a.get("inventory"):  cmd += ["-i", a["inventory"]]
    if a.get("extra_vars"): cmd += ["--extra-vars", a["extra_vars"]]
    if a.get("tags"):       cmd += ["--tags", a["tags"]]
    if a.get("check_mode"): cmd.append("--check")
    return [TextContent(type="text", text=fmt(run_cmd(cmd)))]


async def _run_molecule(a: dict) -> list[TextContent]:
    cmd = ["molecule", a.get("action", "test"), "-s", a.get("scenario", "default")]
    if a.get("driver"): cmd += ["--driver-name", a["driver"]]
    cwd = str(WORKSPACE)
    if a.get("role_path"):
        try:
            cwd = str(safe_path(WORKSPACE, a["role_path"]))
        except ValueError:
            return err("role_path outside workspace")
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
        return err("'filename' required")
    try:
        path = safe_path(inv_dir, fname)
    except ValueError:
        return err("filename outside inventory directory")
    if action == "read":
        return [TextContent(type="text", text=path.read_text() if path.exists() else f"Not found: {fname}")]
    if action == "write":
        path.write_text(a.get("content", ""))
        return [TextContent(type="text", text=f"Written: {path}")]
    if action == "delete":
        path.unlink(missing_ok=True)
        return [TextContent(type="text", text=f"Deleted: {fname}")]
    return err(f"Unknown action: {action}")


async def _check_versions(a: dict) -> list[TextContent]:
    pkgs = a.get("packages") or [
        "ansible", "ansible-core", "molecule", "molecule-libvirt",
        "molecule-podman", "molecule-qemu", "testinfra", "ansible-lint",
    ]
    r = run_cmd(["pip", "show"] + pkgs)
    return [TextContent(type="text", text=r["stdout"] or r["stderr"])]


async def _run_shell(a: dict) -> list[TextContent]:
    cwd = str(WORKSPACE)
    if a.get("cwd"):
        try:
            cwd = str(safe_path(WORKSPACE, a["cwd"]))
        except ValueError:
            return err("cwd outside workspace")
    r = run_cmd(["bash", "-c", a["command"]], cwd=cwd, timeout=a.get("timeout", 60))
    return [TextContent(type="text", text=fmt(r))]


# ─── Inventory Implementations ────────────────────────────────────────────────

async def _ansible_inventory(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    cmd = ["ansible-inventory", "--list"]
    if inv:
        cmd += ["-i", inv]
    r = run_cmd(cmd, env=env)
    if r["returncode"] == 0:
        try:
            data = json.loads(r["stdout"])
            return ok({"ok": True, "inventory": data})
        except json.JSONDecodeError:
            return ok({"ok": True, "stdout": r["stdout"]})
    return ok({"ok": False, "rc": r["returncode"], "stderr": r["stderr"]})


async def _inventory_graph(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    cmd = ["ansible-inventory", "--graph"]
    if inv:
        cmd += ["-i", inv]
    r = run_cmd(cmd, env=env)
    return ok({"ok": r["returncode"] == 0, "graph": r["stdout"], "stderr": r["stderr"]})


async def _inventory_find_host(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    cmd = ["ansible-inventory", "--host", a["host"]]
    if inv:
        cmd += ["-i", inv]
    r = run_cmd(cmd, env=env)
    if r["returncode"] == 0:
        try:
            hostvars = json.loads(r["stdout"])
        except json.JSONDecodeError:
            hostvars = r["stdout"]
        # Also get group membership via --list
        list_r = run_cmd(["ansible-inventory", "--list"] + (["-i", inv] if inv else []), env=env)
        groups: list[str] = []
        if list_r["returncode"] == 0:
            try:
                inv_data = json.loads(list_r["stdout"])
                for grp, gdata in inv_data.items():
                    if grp in ("_meta", "all"):
                        continue
                    if isinstance(gdata, dict) and a["host"] in gdata.get("hosts", []):
                        groups.append(grp)
            except json.JSONDecodeError:
                pass
        return ok({"ok": True, "host": a["host"], "groups": groups, "vars": hostvars})
    return ok({"ok": False, "rc": r["returncode"], "stderr": r["stderr"]})


async def _inventory_diff(a: dict) -> list[TextContent]:
    try:
        path_a = safe_path(WORKSPACE, a["inventory_a"])
        path_b = safe_path(WORKSPACE, a["inventory_b"])
    except ValueError as e:
        return err(f"Inventory path outside workspace: {e}")

    def get_hosts(inv_path: Path) -> dict:
        r = run_cmd(["ansible-inventory", "--list", "-i", str(inv_path)])
        if r["returncode"] != 0:
            return {}
        try:
            return json.loads(r["stdout"])
        except json.JSONDecodeError:
            return {}

    data_a = get_hosts(path_a)
    data_b = get_hosts(path_b)

    def extract_hosts(data: dict) -> set:
        meta = data.get("_meta", {}).get("hostvars", {})
        return set(meta.keys())

    def extract_groups(data: dict) -> set:
        return {k for k in data if k not in ("_meta", "all")}

    hosts_a, hosts_b = extract_hosts(data_a), extract_hosts(data_b)
    groups_a, groups_b = extract_groups(data_a), extract_groups(data_b)

    return ok({
        "ok": True,
        "hosts": {
            "added":   sorted(hosts_b - hosts_a),
            "removed": sorted(hosts_a - hosts_b),
            "common":  sorted(hosts_a & hosts_b),
        },
        "groups": {
            "added":   sorted(groups_b - groups_a),
            "removed": sorted(groups_a - groups_b),
        },
    })


async def _inventory_parse(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    cmd = ["ansible-inventory", "--list"]
    if inv:
        cmd += ["-i", inv]
    r = run_cmd(cmd, env=env)
    if r["returncode"] == 0:
        try:
            data = json.loads(r["stdout"])
            hosts = list(data.get("_meta", {}).get("hostvars", {}).keys())
            groups = [k for k in data if k not in ("_meta", "all")]
            return ok({"ok": True, "hosts": hosts, "groups": groups, "raw": data})
        except json.JSONDecodeError:
            pass
    return ok({"ok": False, "rc": r["returncode"], "stderr": r["stderr"]})


# ─── Playbook Execution Implementations ───────────────────────────────────────

async def _ansible_playbook(a: dict) -> list[TextContent]:
    try:
        playbook = safe_path(WORKSPACE, a["playbook"])
    except ValueError:
        return err("playbook path outside workspace")
    inv, env = _resolve_inv_env(a)
    cwd = _project_cwd(a) or str(WORKSPACE)
    cmd = ["ansible-playbook", str(playbook)]
    if inv:                    cmd += ["-i", inv]
    if a.get("extra_vars"):    cmd += ["--extra-vars", json.dumps(a["extra_vars"])]
    if a.get("tags"):          cmd += ["--tags", a["tags"]]
    if a.get("skip_tags"):     cmd += ["--skip-tags", a["skip_tags"]]
    if a.get("limit"):         cmd += ["--limit", a["limit"]]
    if a.get("check_mode"):    cmd.append("--check")
    if a.get("diff"):          cmd.append("--diff")
    v = int(a.get("verbosity", 0))
    if v > 0:                  cmd.append("-" + "v" * v)
    r = run_cmd(cmd, cwd=cwd, env=env)
    recap = parse_play_recap(r["stdout"])
    return ok({"ok": r["returncode"] == 0, "rc": r["returncode"],
               "recap": recap, "stdout": r["stdout"], "stderr": r["stderr"]})


async def _ansible_task(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    cmd = ["ansible", a["hosts"], "-m", a["module"]]
    if a.get("args"):       cmd += ["-a", a["args"]]
    if inv:                 cmd += ["-i", inv]
    if a.get("become"):     cmd.append("-b")
    conn = a.get("connection", "ssh")
    if conn != "ssh":       cmd += ["-c", conn]
    r = run_cmd(cmd, env=env)
    return ok({"ok": r["returncode"] == 0, "rc": r["returncode"],
               "stdout": r["stdout"], "stderr": r["stderr"]})


async def _ansible_role(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    play = [{
        "hosts": a["hosts"],
        "become": a.get("become", False),
        "vars": a.get("extra_vars") or {},
        "roles": [a["role"]],
    }]
    with temp_playbook(play) as pb_path:
        cmd = ["ansible-playbook", pb_path]
        if inv: cmd += ["-i", inv]
        r = run_cmd(cmd, env=env)
    recap = parse_play_recap(r["stdout"])
    return ok({"ok": r["returncode"] == 0, "rc": r["returncode"],
               "recap": recap, "stdout": r["stdout"], "stderr": r["stderr"]})


async def _validate_playbook(a: dict) -> list[TextContent]:
    try:
        playbook = safe_path(WORKSPACE, a["playbook"])
    except ValueError:
        return err("playbook path outside workspace")
    inv, env = _resolve_inv_env(a)
    cmd = ["ansible-playbook", "--syntax-check", str(playbook)]
    if inv: cmd += ["-i", inv]
    r = run_cmd(cmd, env=env)
    return ok({"ok": r["returncode"] == 0, "rc": r["returncode"],
               "stdout": r["stdout"], "stderr": r["stderr"]})


# ─── Authoring Implementations ────────────────────────────────────────────────

async def _create_playbook(a: dict) -> list[TextContent]:
    try:
        dest = safe_path(WORKSPACE, a["filename"])
    except ValueError:
        return err("filename outside workspace")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(a["content"])
    # Quick syntax validation
    r = run_cmd(["ansible-playbook", "--syntax-check", str(dest)])
    return ok({
        "ok": True,
        "path": str(dest),
        "syntax_ok": r["returncode"] == 0,
        "syntax_errors": r["stderr"] if r["returncode"] != 0 else None,
    })


async def _validate_yaml(a: dict) -> list[TextContent]:
    try:
        path = safe_path(WORKSPACE, a["filename"])
    except ValueError:
        return err("filename outside workspace")
    if not path.exists():
        return err(f"File not found: {a['filename']}")
    try:
        with path.open() as f:
            yaml.safe_load(f)
        return ok({"ok": True, "valid": True, "file": a["filename"]})
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        return ok({
            "ok": False,
            "valid": False,
            "error": str(e),
            "line":   mark.line + 1 if mark else None,
            "column": mark.column + 1 if mark else None,
        })


# ─── Testing Implementations ──────────────────────────────────────────────────

async def _ansible_test_idempotence(a: dict) -> list[TextContent]:
    try:
        playbook = safe_path(WORKSPACE, a["playbook"])
    except ValueError:
        return err("playbook path outside workspace")
    inv, env = _resolve_inv_env(a)
    cmd = ["ansible-playbook", str(playbook)]
    if inv:                 cmd += ["-i", inv]
    if a.get("extra_vars"): cmd += ["--extra-vars", json.dumps(a["extra_vars"])]

    r1 = run_cmd(cmd, env=env)
    recap1 = parse_play_recap(r1["stdout"])

    r2 = run_cmd(cmd, env=env)
    recap2 = parse_play_recap(r2["stdout"])

    changed_on_second = sum(v.get("changed", 0) for v in recap2.values())
    idempotent = r2["returncode"] == 0 and changed_on_second == 0

    return ok({
        "ok": True,
        "idempotent": idempotent,
        "run1": {"rc": r1["returncode"], "recap": recap1},
        "run2": {"rc": r2["returncode"], "recap": recap2, "changed_count": changed_on_second},
    })


# ─── Project Management Implementations ───────────────────────────────────────

async def _register_project(a: dict) -> list[TextContent]:
    try:
        safe_path(WORKSPACE, a["root"])
    except ValueError:
        return err("root path outside workspace")
    data = load_projects()
    data["projects"][a["name"]] = {
        "root":             a["root"],
        "inventory":        a.get("inventory"),
        "roles_path":       a.get("roles_path"),
        "collections_path": a.get("collections_path"),
        "ansible_config":   a.get("ansible_config"),
        "env":              a.get("env") or {},
    }
    if a.get("set_default"):
        data["default"] = a["name"]
    save_projects(data)
    return ok({"ok": True, "registered": a["name"], "default": data["default"]})


async def _list_projects(a: dict) -> list[TextContent]:
    data = load_projects()
    return ok({"ok": True, "projects": data["projects"], "default": data["default"]})


async def _project_playbooks(a: dict) -> list[TextContent]:
    data = load_projects()
    proj = data["projects"].get(a["project"])
    if not proj:
        return err(f"Project not found: {a['project']}")
    try:
        root = safe_path(WORKSPACE, proj["root"])
    except ValueError:
        return err("Project root outside workspace")
    skip_dirs = {".git", "venv", ".venv", "env", ".env", "node_modules", "molecule", ".tox"}
    playbooks: list[str] = []
    for p in root.rglob("*.yml"):
        if any(part in skip_dirs for part in p.parts):
            continue
        try:
            with p.open() as f:
                data_yaml = yaml.safe_load(f)
            if isinstance(data_yaml, list) and data_yaml and isinstance(data_yaml[0], dict) and "hosts" in data_yaml[0]:
                playbooks.append(str(p.relative_to(WORKSPACE)))
        except Exception:
            pass
    return ok({"ok": True, "project": a["project"], "playbooks": playbooks})


async def _project_run_playbook(a: dict) -> list[TextContent]:
    data = load_projects()
    proj = data["projects"].get(a["project"])
    if not proj:
        return err(f"Project not found: {a['project']}")
    env = compose_env(proj)
    try:
        playbook = safe_path(WORKSPACE, a["playbook"])
    except ValueError:
        return err("playbook outside workspace")
    cwd = str(safe_path(WORKSPACE, proj["root"])) if proj.get("root") else str(WORKSPACE)
    cmd = ["ansible-playbook", str(playbook)]
    if proj.get("inventory"):   cmd += ["-i", proj["inventory"]]
    if a.get("extra_vars"):     cmd += ["--extra-vars", json.dumps(a["extra_vars"])]
    if a.get("tags"):           cmd += ["--tags", a["tags"]]
    if a.get("limit"):          cmd += ["--limit", a["limit"]]
    if a.get("check_mode"):     cmd.append("--check")
    r = run_cmd(cmd, cwd=cwd, env=env)
    recap = parse_play_recap(r["stdout"])
    return ok({"ok": r["returncode"] == 0, "rc": r["returncode"],
               "recap": recap, "stdout": r["stdout"], "stderr": r["stderr"]})


async def _project_bootstrap(a: dict) -> list[TextContent]:
    data = load_projects()
    proj = data["projects"].get(a["project"])
    if not proj:
        return err(f"Project not found: {a['project']}")
    env = compose_env(proj)
    cwd = str(safe_path(WORKSPACE, proj["root"])) if proj.get("root") else str(WORKSPACE)
    results: dict = {}
    req = a.get("requirements", "requirements.yml")
    try:
        req_path = safe_path(WORKSPACE, req)
    except ValueError:
        return err("requirements path outside workspace")
    if req_path.exists():
        r = run_cmd(["ansible-galaxy", "install", "-r", str(req_path)], cwd=cwd, env=env, timeout=120)
        results["galaxy_roles"] = {"ok": r["returncode"] == 0, "stdout": r["stdout"], "stderr": r["stderr"]}
        r2 = run_cmd(["ansible-galaxy", "collection", "install", "-r", str(req_path)], cwd=cwd, env=env, timeout=120)
        results["galaxy_collections"] = {"ok": r2["returncode"] == 0, "stdout": r2["stdout"], "stderr": r2["stderr"]}
    else:
        results["galaxy"] = "requirements.yml not found — skipped"
    ver = run_cmd(["ansible", "--version"], cwd=cwd, env=env)
    results["ansible_version"] = ver["stdout"]
    cfg = run_cmd(["ansible-config", "dump", "--only-changed"], cwd=cwd, env=env)
    results["config"] = cfg["stdout"]
    return ok({"ok": True, "project": a["project"], **results})


# ─── Vault Implementations ────────────────────────────────────────────────────

async def _vault_encrypt(a: dict) -> list[TextContent]:
    try:
        path = safe_path(WORKSPACE, a["filename"])
    except ValueError:
        return err("filename outside workspace")
    with vault_password_file(a["password"]) as pf:
        r = run_cmd(["ansible-vault", "encrypt", "--vault-password-file", pf, str(path)])
    return ok({"ok": r["returncode"] == 0, "rc": r["returncode"],
               "stdout": r["stdout"], "stderr": r["stderr"]})


async def _vault_decrypt(a: dict) -> list[TextContent]:
    try:
        path = safe_path(WORKSPACE, a["filename"])
    except ValueError:
        return err("filename outside workspace")
    with vault_password_file(a["password"]) as pf:
        r = run_cmd(["ansible-vault", "decrypt", "--vault-password-file", pf, str(path)])
    return ok({"ok": r["returncode"] == 0, "rc": r["returncode"],
               "stdout": r["stdout"], "stderr": r["stderr"]})


async def _vault_view(a: dict) -> list[TextContent]:
    try:
        path = safe_path(WORKSPACE, a["filename"])
    except ValueError:
        return err("filename outside workspace")
    with vault_password_file(a["password"]) as pf:
        r = run_cmd(["ansible-vault", "view", "--vault-password-file", pf, str(path)])
    return ok({"ok": r["returncode"] == 0, "content": r["stdout"], "stderr": r["stderr"]})


async def _vault_rekey(a: dict) -> list[TextContent]:
    try:
        path = safe_path(WORKSPACE, a["filename"])
    except ValueError:
        return err("filename outside workspace")
    with vault_password_file(a["old_password"]) as old_pf, \
         vault_password_file(a["new_password"]) as new_pf:
        r = run_cmd([
            "ansible-vault", "rekey",
            "--vault-password-file", old_pf,
            "--new-vault-password-file", new_pf,
            str(path),
        ])
    return ok({"ok": r["returncode"] == 0, "rc": r["returncode"],
               "stdout": r["stdout"], "stderr": r["stderr"]})


# ─── Galaxy Implementations ───────────────────────────────────────────────────

async def _galaxy_install(a: dict) -> list[TextContent]:
    try:
        req = safe_path(WORKSPACE, a["requirements"])
    except ValueError:
        return err("requirements path outside workspace")
    _, env = _resolve_inv_env(a)
    cwd = _project_cwd(a) or str(WORKSPACE)
    force = ["--force"] if a.get("force") else []
    r1 = run_cmd(["ansible-galaxy", "role", "install", "-r", str(req)] + force,
                 cwd=cwd, env=env, timeout=120)
    r2 = run_cmd(["ansible-galaxy", "collection", "install", "-r", str(req)] + force,
                 cwd=cwd, env=env, timeout=120)
    return ok({
        "ok": r1["returncode"] == 0 and r2["returncode"] == 0,
        "roles":       {"rc": r1["returncode"], "stdout": r1["stdout"], "stderr": r1["stderr"]},
        "collections": {"rc": r2["returncode"], "stdout": r2["stdout"], "stderr": r2["stderr"]},
    })


async def _galaxy_lock(a: dict) -> list[TextContent]:
    _, env = _resolve_inv_env(a)
    r_roles = run_cmd(["ansible-galaxy", "role", "list"], env=env)
    r_cols  = run_cmd(["ansible-galaxy", "collection", "list"], env=env)
    output_path_str = a.get("output", "galaxy-lock.yml")
    try:
        output_path = safe_path(WORKSPACE, output_path_str)
    except ValueError:
        return err("output path outside workspace")
    lock_data = {
        "roles": r_roles["stdout"],
        "collections": r_cols["stdout"],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    output_path.write_text(yaml.dump(lock_data, default_flow_style=False))
    return ok({"ok": True, "lock_file": output_path_str,
               "roles_ok": r_roles["returncode"] == 0,
               "collections_ok": r_cols["returncode"] == 0})


# ─── Diagnostics Implementations ─────────────────────────────────────────────

async def _ansible_gather_facts(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    args = "gather_subset=all"
    if a.get("filter"):
        args += f" filter={a['filter']}"
    cmd = ["ansible", a["hosts"], "-m", "setup", "-a", args, "-o"]
    if inv: cmd += ["-i", inv]
    r = run_cmd(cmd, env=env, timeout=120)
    return ok({"ok": r["returncode"] == 0, "rc": r["returncode"],
               "stdout": r["stdout"], "stderr": r["stderr"]})


async def _ansible_ping(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    cmd = ["ansible", a["hosts"], "-m", "ping"]
    if inv: cmd += ["-i", inv]
    r = run_cmd(cmd, env=env)
    success = r["returncode"] == 0
    unreachable = "UNREACHABLE" in r["stdout"] or "UNREACHABLE" in r["stderr"]
    return ok({"ok": success, "reachable": success and not unreachable,
               "rc": r["returncode"], "stdout": r["stdout"], "stderr": r["stderr"]})


async def _ansible_remote_command(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    cmd = ["ansible", a["hosts"], "-m", "shell", "-a", a["command"]]
    if inv:             cmd += ["-i", inv]
    if a.get("become"): cmd.append("-b")
    r = run_cmd(cmd, env=env)
    return ok({"ok": r["returncode"] == 0, "rc": r["returncode"],
               "stdout": r["stdout"], "stderr": r["stderr"]})


_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./@:-]+$")


def _validate_remote_path(path: str) -> bool:
    """Reject paths containing shell metacharacters."""
    return bool(_SAFE_PATH_RE.match(path))


async def _ansible_fetch_logs(a: dict) -> list[TextContent]:
    if not _validate_remote_path(a["log_path"]):
        return err("log_path contains invalid characters")
    inv, env = _resolve_inv_env(a)
    lines = int(a.get("lines", 100))
    # Use `command` module for plain tail; pipe to grep only when needed (shell module)
    if a.get("pattern"):
        escaped = a["pattern"].replace("'", "'\\''")
        log_cmd = f"tail -n {lines} {a['log_path']} | grep -E '{escaped}' || true"
        module, mod_args = "shell", log_cmd
    else:
        module, mod_args = "command", f"tail -n {lines} {a['log_path']}"
    cmd = ["ansible", a["hosts"], "-m", module, "-a", mod_args]
    if inv: cmd += ["-i", inv]
    r = run_cmd(cmd, env=env, timeout=60)
    # Simple analysis: count error/warning lines
    error_count   = len(re.findall(r"(?i)error|exception|critical|fatal", r["stdout"]))
    warning_count = len(re.findall(r"(?i)warn", r["stdout"]))
    return ok({
        "ok": r["returncode"] == 0, "rc": r["returncode"],
        "stdout": r["stdout"], "stderr": r["stderr"],
        "analysis": {"errors": error_count, "warnings": warning_count},
    })


_SAFE_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")


async def _ansible_service_manager(a: dict) -> list[TextContent]:
    if not _SAFE_SERVICE_RE.match(a["service"]):
        return err("service name contains invalid characters")
    inv, env = _resolve_inv_env(a)
    action = a["action"]
    if action == "status":
        # Use `command` module — no shell injection risk
        cmd = ["ansible", a["hosts"], "-m", "command",
               "-a", f"systemctl status {a['service']} --no-pager"]
    elif action in ("enable", "disable"):
        # Only toggle enabled; omit state= to avoid invalid value
        cmd = ["ansible", a["hosts"], "-m", "systemd",
               "-a", f"name={a['service']} enabled={'yes' if action == 'enable' else 'no'}",
               "-b"]
    else:
        state_map = {"start": "started", "stop": "stopped", "restart": "restarted"}
        cmd = ["ansible", a["hosts"], "-m", "systemd",
               "-a", f"name={a['service']} state={state_map[action]}",
               "-b"]
    if inv: cmd += ["-i", inv]
    r = run_cmd(cmd, env=env)
    return ok({"ok": r["returncode"] == 0, "rc": r["returncode"],
               "service": a["service"], "action": action,
               "stdout": r["stdout"], "stderr": r["stderr"]})


async def _ansible_diagnose_host(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    checks = {
        "cpu":      f"top -bn1 | grep -E '^%?Cpu'",
        "memory":   "free -m",
        "disk":     "df -h --output=source,size,used,avail,pcent,target | head -20",
        "load":     "cat /proc/loadavg",
        "services": "systemctl list-units --state=failed --no-pager 2>/dev/null | head -20 || true",
        "uptime":   "uptime",
        "network":  "ss -tlnp 2>/dev/null | head -20 || netstat -tlnp 2>/dev/null | head -20 || true",
    }
    results: dict = {}
    for label, shell_cmd in checks.items():
        cmd = ["ansible", a["hosts"], "-m", "shell", "-a", shell_cmd, "-b"]
        if inv: cmd += ["-i", inv]
        r = run_cmd(cmd, env=env, timeout=30)
        results[label] = {"ok": r["returncode"] == 0, "output": r["stdout"], "stderr": r["stderr"]}
    return ok({"ok": True, "hosts": a["hosts"], "checks": results})


async def _ansible_health_monitor(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    samples_count = min(int(a.get("samples", 3)), 10)
    interval = int(a.get("interval", 10))
    samples: list[dict] = []
    for i in range(samples_count):
        if i > 0:
            await asyncio.sleep(interval)
        cmd = ["ansible", a["hosts"], "-m", "shell",
               "-a", "cat /proc/loadavg && free -m | awk 'NR==2{print $3/$2*100}'"]
        if inv: cmd += ["-i", inv]
        r = run_cmd(cmd, env=env, timeout=30)
        samples.append({"sample": i + 1, "ok": r["returncode"] == 0,
                        "output": r["stdout"], "ts": time.strftime("%H:%M:%S")})
    return ok({"ok": True, "hosts": a["hosts"], "samples": samples_count,
               "interval_sec": interval, "data": samples})


# ─── Security & Analysis Implementations ─────────────────────────────────────

async def _ansible_performance_baseline(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    benchmarks = {
        "cpu":    "dd if=/dev/zero bs=1M count=512 2>&1 | tail -1 || true",
        "memory": "free -m",
        "disk_read":  "dd if=/dev/sda bs=1M count=128 of=/dev/null 2>&1 | tail -1 || dd if=/dev/vda bs=1M count=128 of=/dev/null 2>&1 | tail -1 || true",
        "disk_write": "dd if=/dev/zero of=/tmp/_bench_write bs=1M count=128 2>&1 | tail -1 && rm -f /tmp/_bench_write || true",
        "processes":  "ps aux --sort=-%cpu | head -10",
    }
    results: dict = {}
    for label, shell_cmd in benchmarks.items():
        cmd = ["ansible", a["hosts"], "-m", "shell", "-a", shell_cmd, "-b"]
        if inv: cmd += ["-i", inv]
        r = run_cmd(cmd, env=env, timeout=60)
        results[label] = {"ok": r["returncode"] == 0, "output": r["stdout"]}
    return ok({"ok": True, "hosts": a["hosts"], "benchmarks": results})


async def _ansible_capture_baseline(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    snapshot_cmd = (
        "echo '=PROCS=' && ps aux --sort=-%cpu | head -20 && "
        "echo '=NETSTAT=' && ss -tlnp 2>/dev/null | head -20 && "
        "echo '=DISK=' && df -h && "
        "echo '=MEM=' && free -m && "
        "echo '=LOAD=' && cat /proc/loadavg"
    )
    cmd = ["ansible", a["hosts"], "-m", "shell", "-a", snapshot_cmd, "-b"]
    if inv: cmd += ["-i", inv]
    r = run_cmd(cmd, env=env, timeout=60)
    baseline = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hosts": a["hosts"],
        "snapshot": r["stdout"],
    }
    output_str = a.get("output", "baseline.json")
    try:
        output_path = safe_path(WORKSPACE, output_str)
    except ValueError:
        return err("output path outside workspace")
    output_path.write_text(json.dumps(baseline, indent=2))
    return ok({"ok": r["returncode"] == 0, "saved_to": output_str,
               "rc": r["returncode"], "stderr": r["stderr"]})


async def _ansible_compare_states(a: dict) -> list[TextContent]:
    try:
        baseline_path = safe_path(WORKSPACE, a["baseline"])
    except ValueError:
        return err("baseline path outside workspace")
    if not baseline_path.exists():
        return err(f"Baseline file not found: {a['baseline']}")
    baseline = json.loads(baseline_path.read_text())

    inv, env = _resolve_inv_env(a)
    current_cmd = (
        "echo '=PROCS=' && ps aux --sort=-%cpu | head -20 && "
        "echo '=NETSTAT=' && ss -tlnp 2>/dev/null | head -20 && "
        "echo '=DISK=' && df -h && "
        "echo '=MEM=' && free -m && "
        "echo '=LOAD=' && cat /proc/loadavg"
    )
    cmd = ["ansible", a["hosts"], "-m", "shell", "-a", current_cmd, "-b"]
    if inv: cmd += ["-i", inv]
    r = run_cmd(cmd, env=env, timeout=60)

    def extract_section(text: str, section: str) -> set:
        lines = text.split("\n")
        result, in_section = [], False
        for line in lines:
            if f"={section}=" in line:
                in_section = True
                continue
            if line.startswith("=") and "=" in line[1:]:
                in_section = False
            if in_section and line.strip():
                result.append(line.strip())
        return set(result)

    old_snapshot = baseline.get("snapshot", "")
    new_snapshot = r["stdout"]
    old_ports = extract_section(old_snapshot, "NETSTAT")
    new_ports = extract_section(new_snapshot, "NETSTAT")

    return ok({
        "ok": r["returncode"] == 0,
        "baseline_captured_at": baseline.get("captured_at"),
        "drift": {
            "new_listeners": sorted(new_ports - old_ports),
            "removed_listeners": sorted(old_ports - new_ports),
        },
        "current_snapshot": new_snapshot,
    })


async def _ansible_auto_heal(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    symptom = a["symptom"]
    dry_run = a.get("dry_run", True)

    heal_actions: dict = {
        "high_cpu": {
            "diagnose": "ps aux --sort=-%cpu | head -10",
            "fix": "renice +10 $(ps aux --sort=-%cpu | awk 'NR==2{print $2}')",
        },
        "high_memory": {
            "diagnose": "ps aux --sort=-%mem | head -10 && free -m",
            "fix": "sync && echo 3 > /proc/sys/vm/drop_caches",
        },
        "disk_full": {
            "diagnose": "df -h && du -sh /tmp/* /var/log/* 2>/dev/null | sort -rh | head -20",
            "fix": "find /tmp -mtime +7 -delete 2>/dev/null; journalctl --vacuum-size=100M 2>/dev/null || true",
        },
        "service_failed": {
            "diagnose": f"systemctl status {a.get('service', 'unknown')} --no-pager && journalctl -u {a.get('service', 'unknown')} -n 50 --no-pager",
            "fix": f"systemctl restart {a.get('service', 'unknown')}",
        },
        "network_unreachable": {
            "diagnose": "ip route && ip addr && ping -c 3 8.8.8.8 || true",
            "fix": "systemctl restart NetworkManager 2>/dev/null || systemctl restart networking 2>/dev/null || true",
        },
    }
    action = heal_actions.get(symptom, {"diagnose": "echo unknown symptom", "fix": "true"})
    diag_cmd = ["ansible", a["hosts"], "-m", "shell", "-a", action["diagnose"], "-b"]
    if inv: diag_cmd += ["-i", inv]
    r_diag = run_cmd(diag_cmd, env=env, timeout=60)

    r_fix = None
    if not dry_run:
        fix_cmd = ["ansible", a["hosts"], "-m", "shell", "-a", action["fix"], "-b"]
        if inv: fix_cmd += ["-i", inv]
        r_fix = run_cmd(fix_cmd, env=env, timeout=60)

    return ok({
        "ok": True,
        "symptom": symptom,
        "dry_run": dry_run,
        "diagnosis": {"ok": r_diag["returncode"] == 0, "output": r_diag["stdout"]},
        "fix_applied": not dry_run,
        "fix_result": {"ok": r_fix["returncode"] == 0, "output": r_fix["stdout"]} if r_fix else None,
        "fix_command": action["fix"] if dry_run else None,
    })


async def _ansible_network_matrix(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    ports = a.get("ports") or [22, 80, 443]
    port_checks = " && ".join(
        f"(timeout 2 bash -c 'echo >/dev/tcp/localhost/{p}' 2>/dev/null && echo '{p}:open') || echo '{p}:closed'"
        for p in ports
    )
    cmd = ["ansible", a["hosts"], "-m", "shell", "-a", port_checks]
    if inv: cmd += ["-i", inv]
    r = run_cmd(cmd, env=env, timeout=60)

    matrix: dict = {}
    for line in r["stdout"].split("\n"):
        if ":open" in line or ":closed" in line:
            parts = line.strip().split()
            host = parts[0].rstrip("|") if parts else "unknown"
            port_status: dict = {}
            for part in parts[1:]:
                if ":" in part:
                    p, s = part.split(":", 1)
                    port_status[p] = s
            if port_status:
                matrix[host] = port_status

    return ok({"ok": r["returncode"] == 0, "hosts": a["hosts"],
               "ports": ports, "matrix": matrix,
               "stdout": r["stdout"], "stderr": r["stderr"]})


async def _ansible_security_audit(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    audit_cmds: dict = {
        "open_ports": "ss -tlnp 2>/dev/null | grep LISTEN | head -30",
        "ssh_config": "grep -E 'PasswordAuthentication|PermitRootLogin|PubkeyAuthentication' /etc/ssh/sshd_config 2>/dev/null || true",
        "world_writable": "find /etc /usr /bin /sbin -perm -o+w -type f 2>/dev/null | head -20 || true",
        "failed_logins": "grep 'Failed password' /var/log/auth.log 2>/dev/null | tail -20 || grep 'Failed password' /var/log/secure 2>/dev/null | tail -20 || true",
        "sudo_users": "getent group sudo wheel 2>/dev/null || true",
        "suid_files": "find / -perm -4000 -type f 2>/dev/null | head -20 || true",
        "pkg_updates": "apt list --upgradable 2>/dev/null | grep -i security | head -10 || yum check-update --security 2>/dev/null | head -10 || true",
    }
    findings: dict = {}
    for label, shell_cmd in audit_cmds.items():
        cmd = ["ansible", a["hosts"], "-m", "shell", "-a", shell_cmd, "-b"]
        if inv: cmd += ["-i", inv]
        r = run_cmd(cmd, env=env, timeout=60)
        findings[label] = {"ok": r["returncode"] == 0, "output": r["stdout"].strip()}
    return ok({"ok": True, "hosts": a["hosts"], "audit": findings})


async def _ansible_log_hunter(a: dict) -> list[TextContent]:
    inv, env = _resolve_inv_env(a)
    log_paths = a.get("log_paths") or ["/var/log/syslog", "/var/log/messages", "/var/log/auth.log"]
    pattern = a["pattern"].replace("'", "'\\''")
    window = int(a.get("window", 60))

    grep_parts = " ".join(
        f"(grep -E '{pattern}' {lp} 2>/dev/null | head -50) || true"
        for lp in log_paths
    )
    cmd = ["ansible", a["hosts"], "-m", "shell", "-a", grep_parts, "-b"]
    if inv: cmd += ["-i", inv]
    r = run_cmd(cmd, env=env, timeout=120)

    # Parse timestamps and correlate events within the window
    ts_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"   # ISO
        r"|(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})"          # syslog
    )
    matches = ts_pattern.findall(r["stdout"])
    event_count = len([m for m in matches if any(m)])

    return ok({
        "ok": r["returncode"] == 0,
        "pattern": a["pattern"],
        "logs_searched": log_paths,
        "event_count": event_count,
        "correlation_window_sec": window,
        "stdout": r["stdout"],
        "stderr": r["stderr"],
    })


# ─── HTTP/SSE Application ─────────────────────────────────────────────────────

def create_app() -> Starlette:
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
