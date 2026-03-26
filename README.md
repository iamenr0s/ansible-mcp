# ansible-mcp

> An MCP (Model Context Protocol) server that gives Claude AI direct access to your Ansible environment — run playbooks, execute Molecule tests, manage inventory, and drop into a shell, all from a conversation.

![Python](https://img.shields.io/badge/python-3.12-blue)
![Ansible](https://img.shields.io/badge/ansible-10%2B-red)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Table of contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Tools](#tools)
- [Prerequisites](#prerequisites)
- [Quick start (Docker Compose)](#quick-start-docker-compose)
- [Project structure](#project-structure)
- [Configuration](#configuration)
  - [Environment variables](#environment-variables)
  - [ansible.cfg](#ansiblecfg)
  - [SSH keys](#ssh-keys)
- [Kubernetes deployment](#kubernetes-deployment)
  - [Prerequisites](#kubernetes-prerequisites)
  - [Secrets — HashiCorp Vault + ESO](#secrets--hashicorp-vault--eso)
  - [Traefik ingress](#traefik-ingress)
  - [ArgoCD GitOps](#argocd-gitops)
  - [Deploy](#deploy)
- [CI/CD pipeline](#cicd-pipeline)
  - [CI — lint and Molecule tests](#ci--lint-and-molecule-tests)
  - [Release — build, push, deploy](#release--build-push-deploy)
  - [Dependabot](#dependabot)
- [Connecting to Claude.ai](#connecting-to-claudeai)
- [Sample role and Molecule scenario](#sample-role-and-molecule-scenario)
- [Molecule drivers](#molecule-drivers)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

---

## Overview

This project implements an HTTP/SSE MCP server that wraps the Ansible ecosystem inside a Docker container and exposes it as a set of tools Claude can call mid-conversation.

Instead of:
```
you → Slack → teammate → "what's the inventory flag again?" → run → fix → re-run
```

It becomes:
```
you → "run the webservers playbook in check mode against staging"
Claude → runs it, shows you the diff
you → "looks good, run it for real"
Claude → done
```

Claude handles the flags, reads the output, spots failures, and can suggest fixes — all within the same conversation.

---

## Architecture

```
Claude.ai (MCP client)
    │  HTTPS / SSE
    ▼
Traefik ingress  ←── TLS termination, buffering disabled
    │  HTTP/1.1
    ▼
┌─────────────────────────────────────────┐
│  server.py  (Starlette + uvicorn)       │
│  ┌──────────────────────────────────┐   │
│  │  SseServerTransport  /sse        │   │
│  │  MCP SDK Server      /messages/  │   │
│  │  call_tool() dispatcher          │   │
│  └──────────┬───────────────────────┘   │
│             │ subprocess                │
│   ansible-playbook  molecule  bash      │
└─────────────┼───────────────────────────┘
              │ SSH / WinRM
    Managed nodes / infrastructure

GitHub repo
    │  git push
    ▼
GitHub Actions  ──── build & push image ──── GHCR
    │  tag bump
    ▼
ArgoCD  ──── Kustomize overlays ──── Kubernetes
                                       ├── Traefik
                                       ├── External Secrets Operator
                                       └── HashiCorp Vault
```

---

## Tools

| Tool | Description |
|------|-------------|
| `run_playbook` | Runs `ansible-playbook` with optional inventory, extra-vars, tags, and check mode |
| `run_molecule` | Runs `molecule test/converge/verify/destroy/create/lint` against a role |
| `manage_inventory` | Read, write, list, or delete inventory files under `/workspace/inventory/` |
| `check_versions` | Shows installed versions of all managed packages via `pip show` |
| `run_shell` | Runs arbitrary bash commands inside the container workspace |

---

## Prerequisites

| Tool | Min version |
|------|-------------|
| Docker | 24+ |
| Docker Compose | v2 |
| (for K8s) kubectl | 1.28+ |
| (for K8s) ArgoCD | 2.9+ |
| (for K8s) cert-manager | 1.14+ |
| (for K8s) Traefik | 2.10+ |
| (for K8s) External Secrets Operator | 0.9+ |
| (for drivers) KVM/QEMU on host | any |

---

## Quick start (Docker Compose)

### 1. Clone and scaffold

```bash
git clone https://github.com/YOUR_ORG/ansible-mcp.git
cd ansible-mcp
```

Or generate the full project from scratch using the scaffold script:

```bash
bash scaffold-ansible-mcp.sh my-ansible-mcp
cd my-ansible-mcp
```

### 2. Add your SSH key

```bash
mkdir -p ssh
cp ~/.ssh/id_rsa ssh/
chmod 400 ssh/id_rsa
```

### 3. Add your inventory

```bash
cat > workspace/inventory/hosts.ini << 'EOF'
[webservers]
web01 ansible_host=192.168.1.10

[all:vars]
ansible_user=ansible
ansible_python_interpreter=/usr/bin/python3
EOF
```

### 4. Build and run

```bash
docker compose build
docker compose up -d
```

### 5. Verify

```bash
curl -N http://localhost:8000/sse
# → event: endpoint
#   data: /messages/?session_id=...
```

### 6. Connect Claude.ai

Go to **Claude.ai → Settings → Integrations → Add integration** and enter:
```
http://<YOUR_HOST_IP>:8000/sse
```

---

## Project structure

```
ansible-mcp/
├── server.py                          # MCP server (Starlette + SSE)
├── requirements.txt                   # Python dependencies
├── ansible.cfg                        # Ansible configuration
├── Dockerfile
├── docker-compose.yml
├── scaffold-ansible-mcp.sh            # One-command project scaffolder
├── .github/
│   ├── dependabot.yaml
│   └── workflows/
│       ├── ci.yaml                    # Lint + Molecule on PR
│       └── release.yaml               # Build, push, bump prod tag on release
├── k8s/
│   ├── argocd/
│   │   ├── project.yaml               # ArgoCD AppProject
│   │   └── application.yaml           # ArgoCD Application (prod overlay)
│   └── manifests/
│       ├── base/
│       │   ├── kustomization.yaml
│       │   ├── namespace.yaml
│       │   ├── configmap.yaml
│       │   ├── pvc.yaml               # 5Gi workspace volume
│       │   ├── secret-store.yaml      # ESO: Vault SecretStore
│       │   ├── external-secret.yaml   # ESO: ExternalSecret + ServiceAccount
│       │   ├── deployment.yaml
│       │   ├── service.yaml
│       │   ├── ingress.yaml           # Traefik + Let's Encrypt
│       │   └── middleware.yaml        # Traefik SSE middleware
│       └── overlays/
│           ├── dev/
│           │   ├── kustomization.yaml
│           │   └── patch-replicas.yaml
│           └── prod/
│               ├── kustomization.yaml
│               └── patch-resources.yaml
└── workspace/
    ├── inventory/
    │   └── hosts.ini
    └── roles/
        └── example_role/
            ├── tasks/main.yml
            ├── defaults/main.yml
            ├── handlers/main.yml
            └── molecule/
                └── default/
                    ├── molecule.yml   # Podman driver, UBI9 + Debian12
                    ├── converge.yml
                    ├── verify.yml
                    └── tests/
                        └── test_example_role.py
```

---

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `8000` | Bind port |
| `WORKSPACE_DIR` | `/workspace` | Base directory for all file operations |
| `ANSIBLE_CONFIG` | `/etc/ansible/ansible.cfg` | Ansible config file path |

### ansible.cfg

The included `ansible.cfg` is pre-configured with:

- **SSH multiplexing** — `ControlMaster=auto`, `ControlPersist=60s` — speeds up consecutive task runs
- **Fact caching** — JSON file cache with a 1-hour TTL avoids redundant `gather_facts` calls
- **YAML output** — `stdout_callback = yaml` for readable playbook output in Claude's responses
- **Privilege escalation** — `become = True` with `sudo` by default
- **Galaxy servers** — both `galaxy.ansible.com` and `cloud.redhat.com/api/automation-hub/` pre-configured

To override at runtime, mount a custom config:
```yaml
volumes:
  - ./my-ansible.cfg:/etc/ansible/ansible.cfg:ro
```

Or set `ANSIBLE_CONFIG` to a path inside the workspace.

### SSH keys

**Docker Compose** — mount the `./ssh/` directory:
```yaml
volumes:
  - ./ssh:/root/.ssh:ro
```

**Kubernetes** — SSH keys are pulled from HashiCorp Vault by the External Secrets Operator (see [Secrets](#secrets--hashicorp-vault--eso)). No manual `kubectl create secret` required.

---

## Kubernetes deployment

### Kubernetes prerequisites

Ensure the following are running in your cluster before deploying:

- **Traefik** as the ingress controller
- **cert-manager** with a `letsencrypt` `ClusterIssuer`
- **External Secrets Operator** (ESO)
- **ArgoCD**
- **HashiCorp Vault** accessible from the cluster

### Secrets — HashiCorp Vault + ESO

SSH keys are managed entirely through Vault — nothing sensitive ever touches Git.

#### 1. Set up Vault (one-time)

```bash
# Enable KV v2 if not already enabled
vault secrets enable -path=secret kv-v2

# Store the SSH key
vault kv put secret/ansible-mcp/ssh-keys \
  id_rsa=@$HOME/.ssh/id_rsa \
  id_rsa_pub=@$HOME/.ssh/id_rsa.pub

# Create an access policy
vault policy write ansible-mcp - <<'EOF'
path "secret/data/ansible-mcp/*" {
  capabilities = ["read"]
}
EOF

# Enable Kubernetes auth (if not already enabled)
vault auth enable kubernetes

# Configure Kubernetes auth
vault write auth/kubernetes/config \
  kubernetes_host="https://$KUBERNETES_PORT_443_TCP_ADDR:443"

# Bind the role to the service account
vault write auth/kubernetes/role/ansible-mcp \
  bound_service_account_names=ansible-mcp-sa \
  bound_service_account_namespaces=ansible-mcp \
  policies=ansible-mcp \
  ttl=1h
```

#### 2. Update the SecretStore

Edit `k8s/manifests/base/secret-store.yaml` and set your Vault address:

```yaml
spec:
  provider:
    vault:
      server: "https://vault.your-domain.com"   # ← replace this
```

ESO will automatically create the `ansible-ssh-keys` Kubernetes Secret and refresh it every hour.

To force an immediate refresh:
```bash
kubectl annotate externalsecret ansible-ssh-keys \
  force-sync=$(date +%s) --overwrite -n ansible-mcp
```

### Traefik ingress

The ingress is pre-configured for `ansible-mcp.apps.k8s.enros.me` with:

- Let's Encrypt TLS via cert-manager (`tls-acme: "true"`)
- Traefik `websecure` entrypoint
- SSE streaming enabled via the `ansible-mcp-sse-headers` Middleware CRD (`X-Accel-Buffering: no`)

To use a different hostname, update `k8s/manifests/base/ingress.yaml`:
```yaml
spec:
  rules:
    - host: ansible-mcp.your-domain.com   # ← replace
  tls:
    - hosts:
        - ansible-mcp.your-domain.com     # ← replace
      secretName: ansible-mcp-tls
```

> **Important:** The `middleware.yaml` (`X-Accel-Buffering: no`) is required. Without it Traefik buffers the SSE stream and Claude will not receive events in real time.

### ArgoCD GitOps

#### 1. Update the repo URL

In both `k8s/argocd/project.yaml` and `k8s/argocd/application.yaml`, replace:
```yaml
repoURL: https://github.com/YOUR_ORG/ansible-mcp.git
```

#### 2. Register the repo with ArgoCD (if private)

```bash
argocd repo add https://github.com/YOUR_ORG/ansible-mcp.git \
  --username YOUR_USER \
  --password YOUR_TOKEN
```

#### 3. Sync behaviour

The Application is configured with:

| Setting | Value | Effect |
|---------|-------|--------|
| `automated.prune` | `true` | Resources deleted from Git are removed from the cluster |
| `automated.selfHeal` | `true` | Manual changes to the cluster are reverted |
| `syncOptions.CreateNamespace` | `true` | Namespace is created if it doesn't exist |
| `ignoreDifferences` | `/spec/replicas` | Manual replica scaling is not overwritten |
| `retry.limit` | `5` | Retries with exponential backoff up to 3 minutes |

### Deploy

#### 1. Update the image registry

In `k8s/manifests/overlays/prod/kustomization.yaml`:
```yaml
images:
  - name: your-registry/ansible-mcp
    newTag: "1.0.0"   # ← set your initial tag
```

#### 2. Build and push the image

```bash
docker build -t ghcr.io/YOUR_ORG/ansible-mcp:1.0.0 .
docker push ghcr.io/YOUR_ORG/ansible-mcp:1.0.0
```

#### 3. Apply ArgoCD manifests

```bash
kubectl apply -f k8s/argocd/project.yaml
kubectl apply -f k8s/argocd/application.yaml
```

ArgoCD begins syncing immediately. Monitor progress:

```bash
argocd app get ansible-mcp
argocd app wait ansible-mcp --health
kubectl get all -n ansible-mcp
```

#### 4. Connect Claude.ai

```
https://ansible-mcp.apps.k8s.enros.me/sse
```

---

## CI/CD pipeline

### CI — lint and Molecule tests

Runs on every pull request and push to `main`:

```
PR opened / push to main
    ├── ansible-lint (example_role)
    ├── kustomize overlay validation (dev + prod)
    └── molecule test -s default (Podman driver, UBI9 + Debian12)
```

### Release — build, push, deploy

Triggered when a GitHub Release is published (e.g. `v1.2.3`):

```
Release published
    ├── Docker image built with Buildx (GHA layer cache)
    ├── Pushed to GHCR:
    │     ghcr.io/YOUR_ORG/ansible-mcp:1.2.3
    │     ghcr.io/YOUR_ORG/ansible-mcp:1.2
    │     ghcr.io/YOUR_ORG/ansible-mcp:sha-abc1234
    ├── kustomize edit set image patches overlays/prod/kustomization.yaml
    ├── Bot commits newTag: "1.2.3" to main  [skip ci]
    └── ArgoCD detects change → rolling update
```

#### Required permissions

Go to **Repo → Settings → Actions → General → Workflow permissions** and set **Read and write permissions**. This allows the bot to commit the tag bump.

#### Using a private registry

Replace the GHCR login step in `.github/workflows/release.yaml` with:
```yaml
- name: Log in to private registry
  uses: docker/login-action@v3
  with:
    registry: your-registry.example.com
    username: ${{ secrets.REGISTRY_USER }}
    password: ${{ secrets.REGISTRY_PASSWORD }}
```

Then add `REGISTRY_USER` and `REGISTRY_PASSWORD` to your repository secrets.

### Dependabot

Automated dependency updates run every Monday at 08:00 UTC across three ecosystems:

| Ecosystem | Group | Packages |
|-----------|-------|---------|
| GitHub Actions | `github-actions-all` | All actions — single weekly batch PR |
| pip | `ansible-all` | ansible, ansible-core, molecule, molecule-*, testinfra |
| pip | `mcp-server` | mcp, uvicorn, starlette |
| Docker | — | `python:3.12-slim` base image |

Ansible and Molecule are grouped together so breaking changes are caught by a single Molecule CI run before merging.

---

## Connecting to Claude.ai

1. Open **Claude.ai → Settings → Integrations → Add integration**
2. Enter the SSE URL:
   - **Docker Compose:** `http://<YOUR_HOST_IP>:8000/sse`
   - **Kubernetes:** `https://ansible-mcp.apps.k8s.enros.me/sse`
3. Claude auto-discovers all five tools

---

## Sample role and Molecule scenario

The repo includes `workspace/roles/example_role/` as a working reference:

```
example_role/
├── tasks/main.yml       # installs packages, starts a service
├── defaults/main.yml    # example_role_packages, example_role_service
├── handlers/main.yml    # restart handler
└── molecule/default/
    ├── molecule.yml     # Podman driver, UBI9 + Debian 12 platforms
    ├── converge.yml     # applies the role
    ├── verify.yml       # runs testinfra
    └── tests/
        └── test_example_role.py
```

Run the scenario locally:
```bash
cd workspace/roles/example_role
molecule test
```

Or ask Claude:
> *"Run the molecule default scenario for example_role using the podman driver"*

---

## Molecule drivers

| Driver | Works out of the box | Notes |
|--------|---------------------|-------|
| `podman` | Yes | `podman` is installed in the image |
| `libvirt` | No | Requires `libvirt-dev` in the Dockerfile and KVM on the host |
| `qemu` | No | Requires KVM on the host |

To enable `libvirt` / `qemu` drivers, uncomment in `docker-compose.yml`:
```yaml
privileged: true
devices:
  - /dev/kvm:/dev/kvm
```

And add `libvirt-dev` to the `apt-get` block in the Dockerfile:
```dockerfile
RUN apt-get install -y --no-install-recommends \
    ... \
    libvirt-dev
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `curl /sse` returns nothing | Check `docker compose logs ansible-mcp` — uvicorn should show `Started server process` |
| Claude can't reach the SSE endpoint | Ensure port 8000 is open; for K8s check the Traefik ingress and middleware are applied |
| SSE drops immediately on Traefik | Verify `middleware.yaml` is applied and the ingress annotation references `ansible-mcp-sse-headers@kubernetescrd` |
| SSH key not found | For Docker: check `./ssh/id_rsa` exists and is `chmod 400`. For K8s: run `kubectl describe externalsecret ansible-ssh-keys -n ansible-mcp` |
| Vault auth failing | Confirm `vault write auth/kubernetes/role/ansible-mcp` binds `ansible-mcp-sa` in namespace `ansible-mcp` |
| ESO not syncing | Check `SecretSyncedError` conditions with `kubectl describe externalsecret ansible-ssh-keys -n ansible-mcp` |
| `molecule-libvirt` install fails | Add `libvirt-dev` to `apt-get` in the Dockerfile, rebuild |
| KVM permission denied | Add `--device /dev/kvm` or set `privileged: true` |
| Molecule times out in K8s | Increase `resources.limits` in the overlay patch and molecule `timeout` in `molecule.yml` |
| ArgoCD sync stuck | Run `argocd app get ansible-mcp` for error details; force with `argocd app sync --force` |
| ArgoCD can't pull repo | Run `argocd repo add` with valid credentials before applying the Application manifest |

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Make your changes and ensure CI passes (`molecule test`, `ansible-lint`)
4. Open a pull request against `main`

---

## License

MIT — see [LICENSE](LICENSE).
