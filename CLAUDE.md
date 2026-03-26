# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

An Ansible MCP (Model Context Protocol) server — a containerized service that exposes Ansible/Molecule operations via the MCP protocol over HTTP/SSE. MCP clients (like Claude) connect to it and can run playbooks, test roles, manage inventory, and execute shell commands on managed infrastructure.

## Commands

### Run locally (no Docker)
```bash
pip install -r requirements.txt
python server.py
```
Server starts on `http://0.0.0.0:8000`. Override with env vars: `MCP_HOST`, `MCP_PORT`, `WORKSPACE_DIR`.

### Docker
```bash
docker-compose up -d          # start
docker-compose logs -f        # follow logs
docker-compose down           # stop
```

### Kubernetes (kustomize)
```bash
kubectl kustomize k8s/manifests/overlays/dev | kubectl apply -f -
kubectl kustomize k8s/manifests/overlays/prod | kubectl apply -f -
```

### Validate kustomize overlays (CI check)
```bash
kubectl kustomize k8s/manifests/overlays/dev > /dev/null
kubectl kustomize k8s/manifests/overlays/prod > /dev/null
```

### Lint Ansible roles
```bash
ansible-lint workspace/roles/example_role
```

### Run Molecule tests
```bash
cd workspace/roles/example_role
molecule test -s default       # full test cycle (podman driver)
molecule converge -s default   # converge only
molecule verify -s default     # verify only
molecule destroy -s default    # cleanup
```

## Architecture

**`server.py`** is the entire application. It implements an MCP server using Starlette + Uvicorn with SSE transport. The five exposed MCP tools are:

| Tool | What it does |
|------|-------------|
| `run_playbook` | Executes `ansible-playbook` with optional inventory, extra-vars, tags, check mode |
| `run_molecule` | Runs `molecule <action>` for a role with configurable driver (podman/libvirt/qemu) |
| `manage_inventory` | CRUD on inventory files under `workspace/inventory/` |
| `check_versions` | Reports versions of ansible, molecule, testinfra, etc. |
| `run_shell` | Runs arbitrary shell commands inside the container workspace |

All operations are scoped to `/workspace` (configurable via `WORKSPACE_DIR`).

**`ansible.cfg`** — Ansible is pre-configured with:
- Inventory at `/workspace/inventory`, roles at `/workspace/roles`
- SSH via `/root/.ssh/id_rsa`, host key checking disabled, ControlMaster persist
- JSON fact cache at `/tmp/ansible_facts_cache` (1h TTL)
- Log output to `/workspace/ansible.log`

**`workspace/`** — volume-mounted persistent storage containing inventory files, roles, and logs. In Docker, `./workspace` is bind-mounted to `/workspace`. In Kubernetes, a PVC is used.

**`k8s/`** uses kustomize overlays (dev/prod) over a shared base with ArgoCD integration.

## CI/CD

GitHub Actions (`.github/workflows/ci.yaml`) runs two jobs on push/PR:
1. **lint** — `ansible-lint` on `example_role` + kustomize overlay validation
2. **molecule** — `molecule test` with podman driver (depends on lint passing)

Dependabot is configured to auto-update GitHub Actions, pip packages (grouped by project), and Docker base images weekly.

## SSH Keys

Place SSH keys in `./ssh/` locally — they are bind-mounted read-only to `/root/.ssh` inside the container. The `./ssh/` directory is gitignored.
