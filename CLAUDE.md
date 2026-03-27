# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Workflow Orchestration

### 1. Plan node default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decision)
- If something goes sideaways, STOP and re-plan immediately - don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-improvement Loop
- After any connection from the user: update `tasks/LESSONS.md` with the pattern
- Write rules for yourself that prevent the same mistacke
- Ruthlessly iterate on these lessons until mistacke rate drops
- Review lessons at session start for relevant project

### 4. Verfication before done
- Never mark a task complete without proving it works
- Diff behaviour between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand elegance (balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- if a fix feels hacky: "Knowning everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes - don't over-engineer
- Challange your own work before presenting it

### 6. Autonomous bug fixing
- When give a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests - then resolve them
- Zero context switching required for the user
- Go fix failing CI tests without being told how

## Task management

1. **Plan first**: Write plan to `tasks/TODO.md` with checkable items
2. **Verify plan**: Check in before starting implementation
3. **Track progress**: Mark items complete as you go
4. **Explain changes**: High-level summary at each step
5. **Document results**: Add review section to `tasks/TODO.md`
6. **Capture lesson**: Update `tasks/LESSONS..md` after corrections

## Core principles

- **Simplicity first**: Make every change as simple as possible. Impact minimal code.
- **No laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal impact**: Changes should only touch what's necessary. Avoid introducing bugs.

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

**`server.py`** is the entire application (~200 lines). It implements an MCP server using Starlette + Uvicorn with SSE transport. All subprocess execution flows through `run_cmd()` — a synchronous blocking wrapper around `subprocess.run`. The five exposed MCP tools are:

| Tool | What it does | Timeout |
|------|-------------|---------|
| `run_playbook` | Executes `ansible-playbook` with optional inventory, extra-vars, tags, check mode | 300s |
| `run_molecule` | Runs `molecule <action>` for a role with configurable driver (podman/libvirt/qemu) | 600s |
| `manage_inventory` | read/write/list/delete inventory files under `workspace/inventory/` | 300s |
| `check_versions` | Reports versions of ansible, molecule, testinfra, etc. via `pip show` | 300s |
| `run_shell` | Runs arbitrary `bash -c` commands inside the container workspace | 60s (configurable via `timeout` arg) |

All operations are scoped to `/workspace` (configurable via `WORKSPACE_DIR`).

**`ansible.cfg`** — Ansible is pre-configured with:
- Inventory at `/workspace/inventory`, roles at `/workspace/roles`
- SSH via `/root/.ssh/id_rsa`, host key checking disabled, ControlMaster persist
- JSON fact cache at `/tmp/ansible_facts_cache` (1h TTL)
- Log output to `/workspace/ansible.log`

**`workspace/`** — volume-mounted persistent storage containing inventory files, roles, and logs. In Docker, `./workspace` is bind-mounted to `/workspace`. In Kubernetes, a PVC is used.

**`k8s/`** uses kustomize overlays (dev/prod) over a shared base with ArgoCD integration.

**Security note:** The SSE endpoint has no authentication. Anyone with network access to port 8000 can invoke all tools. Restrict network access at the infrastructure level (firewall, ingress auth, VPN).

**Traefik note:** The `k8s/manifests/base/middleware.yaml` sets `X-Accel-Buffering: no` — this is required for SSE streaming. Without it Traefik buffers the stream and Claude never receives events.

## MCP Client Connection

Connect an MCP client (e.g. Claude Desktop) to `http://<host>:8000/sse`. The SSE endpoint is `/sse`; the POST message endpoint is `/messages/`.

## CI/CD

GitHub Actions (`.github/workflows/ci.yaml`) runs two jobs on push/PR:
1. **lint** — `ansible-lint` on `example_role` + kustomize overlay validation
2. **molecule** — `molecule test` with podman driver (depends on lint passing)

**Release workflow** (`.github/workflows/release.yaml`): publishing a GitHub Release triggers a Docker build pushed to `ghcr.io/<owner>/ansible-mcp` with semver tags, then auto-commits a prod overlay image tag bump to `k8s/manifests/overlays/prod/kustomization.yaml` (ArgoCD picks this up). Requires **Read and write** workflow permissions in repo settings.

Dependabot is configured to auto-update GitHub Actions, pip packages (grouped by project), and Docker base images weekly.

## SSH Keys

Place SSH keys in `./ssh/` locally — they are bind-mounted read-only to `/root/.ssh` inside the container. The `./ssh/` directory is gitignored.

In Kubernetes, SSH keys are pulled from HashiCorp Vault by the External Secrets Operator (ESO) — configure the Vault address in `k8s/manifests/base/secret-store.yaml`.
