# ai-dev-tools

[![Build](https://github.com/bhatti/ai-dev-tools/actions/workflows/build-push.yml/badge.svg)](https://github.com/bhatti/ai-dev-tools/actions/workflows/build-push.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

AI-powered SDLC automation that runs anywhere — standalone Python, Docker, Kubernetes, or Formicary. Picks issues from GitHub or Jira, plans and implements changes using Claude Code, creates pull requests, and responds to review feedback.

Each step is a small idempotent Python script. Steps communicate via `/workspace/{issue-id}/` JSON files — no framework lock-in. The same scripts run locally, in Docker Compose, in a K8s Job, or as a Formicary task.

## Architecture

```
issue_picker → plan → implement → create_pr → monitor_pr → learn
     ↓             ↓          ↓           ↓            ↓         ↓
issue.json   plan.md   impl_result  pr.json  monitor_result  learnings.md
             plan_result.json
```

In Kubernetes, `plan/implement/create_pr` run as init containers (sequential, must succeed); `monitor_pr` runs as the main container. `learn` is called automatically by `monitor_pr` when the PR is merged/closed.

## Quick Start — Docker (one step at a time)

### 1. Prerequisites

- Docker installed and running
- A GitHub repo with at least one issue labeled `ai-ready`
- A [GitHub PAT](https://github.com/settings/tokens) with `repo` + `issues` scope
- Claude API key (Anthropic) or AWS Bedrock access

### 2. Build the image

```bash
git clone https://github.com/bhatti/ai-dev-tools.git
cd ai-dev-tools
docker build -t ai-dev-tools:local .
```

> **Tip:** The build takes ~3 min the first time (installs gh CLI, jira CLI, claude-code, codex).
> Subsequent builds use the Docker layer cache.

### 3. Set environment variables

The simplest approach is to export env vars in your shell — no `.env` file needed:

```bash
export GH_ORG=bhatti
export GH_REPO=todo-sample
export GH_TOKEN=ghp_your_token_here
export AI_MODEL=claude-sonnet-4-6

# If using Anthropic API directly:
export ANTHROPIC_API_KEY=sk-ant-your_key_here

# If using AWS Bedrock:
# export ANTHROPIC_BEDROCK_BASE_URL=http://ai/bedrock
# export CLAUDE_CODE_USE_BEDROCK=1
# export CLAUDE_CODE_SKIP_BEDROCK_AUTH=1
```

Or copy `.env.example` to `.env` and fill it in (`.env` is git-ignored):

```bash
cp .env.example .env
# Edit .env — never commit it
```

### 4. Create workspace and secrets dirs

```bash
mkdir -p test-workspace secrets
# Optional: copy SSH key if your repo requires it
# cp ~/.ssh/id_rsa secrets/ssh-key
```

### 5. Run each step

> Replace `3` with your actual issue number throughout.

**Step 1 — Issue picker** (transitions label `ai-ready` → `ai-in-progress`, writes `issue.json`)

```bash
docker compose run --rm gh-issue-picker
# Verify:
cat test-workspace/3/issue.json
```

**Step 2 — Plan** (Claude decomposes issue into tasks, writes `plan.md`)

```bash
ISSUE_ID=3 docker compose run --rm gh-plan
# Verify:
cat test-workspace/3/plan.md
cat test-workspace/3/plan_result.json
```

**Step 3 — Implement** (clones repo, creates branch, Claude writes code, commits and pushes)

```bash
ISSUE_ID=3 docker compose run --rm gh-implement
# Verify:
cat test-workspace/3/impl_result.json
# Check GitHub: a branch named ai/3-... should appear in your repo
```

**Step 4 — Create PR**

```bash
ISSUE_ID=3 docker compose run --rm gh-create-pr
# Verify:
cat test-workspace/3/pr.json   # contains the PR URL
```

**Step 5 — Monitor PR** (polls every 2 min, responds to review comments, calls learn on merge)

```bash
ISSUE_ID=3 docker compose run --rm gh-monitor-pr
# Long-running — exits 0 when PR is merged or closed
```

**Step 6 — Learn** (can also be run standalone without waiting for monitor)

```bash
ISSUE_ID=3 docker compose run --rm gh-learn
cat test-workspace/3/learnings.md
```

### Makefile shortcuts

```bash
make gh-pick                      # issue picker
make gh-plan ISSUE_ID=3           # plan
make gh-implement ISSUE_ID=3      # implement
make gh-pr ISSUE_ID=3             # create PR
make gh-monitor ISSUE_ID=3        # monitor PR
make gh-learn ISSUE_ID=3          # extract learnings

make gh-all ISSUE_ID=3            # all 6 steps in sequence
```

### Re-running a failed step

All steps are idempotent: they check for a completed output file and skip if already done. To force a re-run, delete the checkpoint:

```bash
rm test-workspace/3/plan_result.json   # force re-plan
rm test-workspace/3/impl_result.json   # force re-implement
rm test-workspace/3/pr.json            # force re-create-pr
```

### Drop into a shell for debugging

```bash
docker compose run --rm --entrypoint bash gh-plan
# Inside container:
python -m scripts.gh.plan --issue-id 3
cat /workspace/3/plan.md
```

---

## Publishing the Docker Image

### Option A — GitHub Container Registry (automatic via CI)

Pushing to `main` automatically builds and pushes to `ghcr.io/bhatti/ai-dev-tools` via the GitHub Actions workflow in `.github/workflows/build-push.yml`. Tags generated:
- `main` — latest commit on main
- `sha-<short-sha>` — every commit
- `v1.2.3` / `1.2` — when you push a semver tag

```bash
# Trigger a versioned release:
git tag v1.0.0 && git push origin v1.0.0
```

### Option B — Manual push to GHCR

```bash
# Authenticate once:
echo $GH_TOKEN | docker login ghcr.io -u bhatti --password-stdin

# Build and push:
make build push IMAGE=ghcr.io/bhatti/ai-dev-tools TAG=latest
# or explicitly:
docker build -t ghcr.io/bhatti/ai-dev-tools:latest .
docker push ghcr.io/bhatti/ai-dev-tools:latest
```

### Option C — Docker Hub

```bash
docker login   # prompts for Docker Hub credentials

make build push IMAGE=bhatti/ai-dev-tools TAG=latest
# or:
docker build -t bhatti/ai-dev-tools:latest .
docker push bhatti/ai-dev-tools:latest
```

### Option D — Private registry (ECR, GCR, ACR, etc.)

```bash
# Example: AWS ECR
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin 123456789.dkr.ecr.us-east-1.amazonaws.com

make build push \
  IMAGE=123456789.dkr.ecr.us-east-1.amazonaws.com/ai-dev-tools \
  TAG=latest
```

---

## Workflow Artifact Reference

All steps read/write files under `/workspace/{issue-id}/`:

| File | Written by | Read by |
|------|-----------|---------|
| `issue.json` | issue_picker | plan, implement, create_pr |
| `plan.md` | plan | implement |
| `plan_result.json` | plan | — (idempotency check) |
| `impl_result.json` | implement | create_pr, monitor_pr |
| `branch.txt` | implement | create_pr |
| `pr.json` | create_pr | monitor_pr, learn |
| `processed_comments.json` | monitor_pr | monitor_pr (dedup) |
| `monitor_result.json` | monitor_pr | — |
| `learnings.md` | learn | — |
| `logs/` | all steps | debugging |

---

## Configuration

All configuration is via environment variables. The `.env` file (git-ignored) is optional — you can export vars directly in your shell. See [docs/configuration.md](docs/configuration.md) for the full reference.

Key variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GH_ORG` | Yes | — | GitHub org or user |
| `GH_REPO` | Yes | — | GitHub repo name |
| `GH_TOKEN` | Yes | — | PAT with `repo` + `issues` scope |
| `AI_MODEL` | No | `claude-sonnet-4-6` | Claude model |
| `PICKUP_LABEL` | No | `ai-ready` | Label that triggers pickup |
| `ANTHROPIC_API_KEY` | Yes* | — | Anthropic API key (direct) |
| `ANTHROPIC_BEDROCK_BASE_URL` | Yes* | `http://ai/bedrock` | Bedrock endpoint |

*One of `ANTHROPIC_API_KEY` or Bedrock vars is required.

---

## Docker Image Contents

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.12 | Script runtime |
| Node.js | LTS | claude-code and codex CLI |
| [claude-code](https://github.com/anthropics/claude-code) | latest | AI coding |
| [@openai/codex](https://github.com/openai/codex) | latest | Alternative AI coding CLI |
| [gh CLI](https://cli.github.com/) | 2.62.0 | GitHub operations |
| [jira CLI](https://github.com/ankitpokhrel/jira-cli) | 1.5.2 | Jira operations |
| [you-got-skills](https://github.com/bhatti/you-got-skills) | latest | Claude skills (installed at startup) |

---

## Supported Platforms

| Platform | Issue source | PR target |
|----------|-------------|-----------|
| GitHub | GitHub Issues | GitHub PRs via `gh` CLI |
| Jira | Jira Issues (JQL) | BitBucket PRs via REST API |

---

## Docs

- [Setup Guide](docs/setup.md) — local dev, Docker testing, label setup
- [Configuration Reference](docs/configuration.md) — all env vars
- [Architecture](docs/architecture.md) — design and data flow
- [Kubernetes Deployment](docs/k8s-deployment.md) — K8s Jobs, CronJobs, PVC, RBAC
- [Formicary Integration](docs/formicary-integration.md) — running via Formicary

## Development

```bash
# Install dev deps
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run tests (44 tests)
make test

# Run with coverage
make test-cov

# Clean build artifacts
make clean
```

## License

MIT — see [LICENSE](LICENSE).
