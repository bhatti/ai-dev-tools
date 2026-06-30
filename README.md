# ai-dev-tools

[![Build](https://github.com/bhatti/ai-dev-tools/actions/workflows/build-push.yml/badge.svg)](https://github.com/bhatti/ai-dev-tools/actions/workflows/build-push.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

AI-powered SDLC automation that runs anywhere — standalone Python, Docker, Kubernetes, or Formicary. Picks issues from GitHub or Jira, plans and implements changes using Claude Code, creates pull requests, and responds to review feedback.

Each step is a small idempotent Python script. Steps communicate via `/workspace/{issue-id}/` JSON files — no framework lock-in. The same scripts run locally, in Docker Compose, in a K8s Job, or as a Formicary task.

## Architecture

```
issue_picker → plan → implement → create_pr → poll_pr → learn
     ↓            ↓          ↓           ↓          ↓         ↓
issue.json   plan.md   impl_result  pr.json  poll_result  learnings.md
             plan_result.json
```

In Kubernetes, `plan/implement/create_pr` run as init containers (sequential, must succeed); `poll_pr` runs as the main container. `learn` is called automatically by `poll_pr` when the PR is merged/closed.

## Quick Start — Docker (one step at a time)

### 1. Prerequisites

- Docker installed and running
- A GitHub repo with at least one issue labeled `ai-ready`
- A [GitHub PAT](https://github.com/settings/tokens) with `repo` + `issues` scope
- Claude API key (Anthropic) or AWS Bedrock access

### 2. Get the image

**Option A — Pull from Docker Hub (fastest):**

```bash
docker pull plexobject/ai-dev-tools:latest
```

**Option B — Build locally:**

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

**Step 5 — Poll PR** (polls every 2 min, responds to `ai-bot` review comments, exits on merge/close)

```bash
ISSUE_ID=3 docker compose run --rm gh-poll-pr
# Long-running — exits 0 when PR is merged or closed
```

**Step 6 — Learn** (can also be run standalone without waiting for poll)

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
make gh-poll ISSUE_ID=3           # poll PR for comments/merge
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

## Jira / BitBucket Pipeline

The same SDLC pipeline works with Jira issues and BitBucket PRs:

### Environment

```bash
export JIRA_PROJECT=PROJ
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=your_jira_api_token
export JIRA_BASE_URL=https://yourorg.atlassian.net
# BitBucket account username (NOT email) — find at bitbucket.org/account/settings/
export BITBUCKET_USERNAME=your-bb-username
export BITBUCKET_WORKSPACE=your-workspace
# Atlassian HTTP Access Token (ATATT...) — works for both REST API and git clone
export BITBUCKET_TOKEN=ATATT_your_token_here
export BITBUCKET_REPO=your-repo
```

### Run each step

```bash
# Pick issue (transitions label ai-ready → ai-in-progress)
docker compose run --rm jira-issue-picker

# Plan
ISSUE_ID=PROJ-42 docker compose run --rm jira-plan

# Implement (clones via HTTPS token, branches, codes, commits)
ISSUE_ID=PROJ-42 docker compose run --rm jira-implement

# Create BitBucket PR
ISSUE_ID=PROJ-42 docker compose run --rm jira-create-pr

# Poll PR (responds to ai-bot comments, exits on merge/decline)
ISSUE_ID=PROJ-42 docker compose run --rm jira-poll-pr

# Learn
ISSUE_ID=PROJ-42 docker compose run --rm jira-learn
```

### Makefile shortcuts

```bash
make jira-pick
make jira-plan ISSUE_ID=PROJ-42
make jira-implement ISSUE_ID=PROJ-42
make jira-pr ISSUE_ID=PROJ-42
make jira-poll ISSUE_ID=PROJ-42
make jira-learn ISSUE_ID=PROJ-42

make jira-all ISSUE_ID=PROJ-42    # all steps in sequence
```

### Repo routing via issue labels

By default the pipeline clones `BITBUCKET_WORKSPACE/BITBUCKET_REPO`. To route a specific Jira issue to a different repo, add a label:

```
repo:my-repo              # uses main branch
repo:my-repo:develop      # uses develop branch
```

The workspace always comes from the `BITBUCKET_WORKSPACE` env var.

### Poll PR comment protocol

The poll step only responds to PR comments that start with `ai-bot` (case-insensitive). All other comments are ignored. This prevents the bot from responding to its own replies or unrelated human discussion.

---

## Docker Image

The official image is published to Docker Hub:

```bash
docker pull plexobject/ai-dev-tools:latest
```

### Versioned releases

Use `make release` to bump the patch version, tag, and push:

```bash
make release    # bumps VERSION (0.1.1 → 0.1.2), commits, tags v0.1.2, pushes
make tag        # tags current VERSION without bumping
```

GitHub Actions (`.github/workflows/build-push.yml`) also builds on push to `main` and on semver tags, publishing to `ghcr.io/bhatti/ai-dev-tools`.

### Manual push

```bash
# Docker Hub:
docker login
make build push IMAGE=plexobject/ai-dev-tools TAG=latest

# GHCR:
echo $GH_TOKEN | docker login ghcr.io -u bhatti --password-stdin
make build push IMAGE=ghcr.io/bhatti/ai-dev-tools TAG=latest

# Private registry (ECR, GCR, ACR):
make build push IMAGE=123456789.dkr.ecr.us-east-1.amazonaws.com/ai-dev-tools TAG=latest
```

---

## Workflow Artifact Reference

All steps read/write files under `/workspace/{issue-id}/`:

| File | Written by | Read by |
|------|-----------|---------|
| `issue.json` | issue_picker | plan, implement, create_pr |
| `plan.md` | plan | implement |
| `plan_result.json` | plan | — (idempotency check) |
| `impl_result.json` | implement | create_pr, poll_pr |
| `branch.txt` | implement | create_pr |
| `pr.json` | create_pr | poll_pr, learn |
| `processed_comments.json` | poll_pr | poll_pr (comment dedup) |
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

# Run tests (48 tests)
make test

# Run with coverage
make test-cov

# Clean build artifacts
make clean
```

## License

MIT — see [LICENSE](LICENSE).
