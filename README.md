# ai-dev-tools

[![Build](https://github.com/bhatti/ai-dev-tools/actions/workflows/build-push.yml/badge.svg)](https://github.com/bhatti/ai-dev-tools/actions/workflows/build-push.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

AI-powered SDLC automation that runs anywhere — standalone Python, Docker, Kubernetes, or Formicary. Picks issues from GitHub or Jira, plans and implements changes using Claude Code, creates pull requests, and responds to review feedback.

Each step is a small idempotent Python script. Steps communicate via `/workspace/{issue-id}/` JSON files — no framework lock-in. The same scripts run locally, in Docker Compose, in a K8s Job, or as a Formicary task.

## Architecture

**Implement pipeline:**
```
issue_picker → plan → implement → create_pr → poll_pr → learn
     ↓            ↓          ↓           ↓          ↓         ↓
issue.json   plan.md   impl_result  pr.json  poll_result  learnings.md
             plan_result.json
```

**Review pipeline (signal-based pause):**
```
review → post_findings ──(PAUSE_JOB)──→ apply_feedback
           └─ Block Kit to Slack           └─ Decision env var (injected on resume)
           └─ exits 3
```

**Ad-hoc + Slack router:**
```
Slack mention → router → formicary submit → adhoc/run_skill → Slack thread reply
                       ↳ resume paused job (thread reply or Block Kit button click)
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

## Issue Analysis with Git Archaeology

The `jira-analyze` (`ai-jira-query` Mode=analyze) and `gh-analyze` (`ai-jira-query` Mode=analyze, DefaultTracker=github) workflows go beyond reading the issue text — they clone the associated repository and run a git history pass to surface root-cause signals.

### What it does

When a Bitbucket repo (`BITBUCKET_WORKSPACE` + `BITBUCKET_REPO`) or GitHub repo (`GH_ORG` + `GH_REPO`) is configured, the analyze step:

1. **Clones the repo** at depth 50 into `workspace/repo_cache/` using HTTPS (token) or SSH fallback — matching the same credential pattern as the implement/clone steps
2. **Finds related commits** — `git log --grep=<issue-key>` for the last 10 matching commits
3. **Ranks hot files** — change-count per file across the last 50 commits; top 5 shown with a "hot file" marker when change-count ≥ 10
4. **Shows recent hot-file changes** — last 10 commits touching the three most-changed files

The git context is appended to the Claude prompt as a Markdown block and capped at 3000 characters to keep prompts a reasonable size.

### Skill matching

Before running plain analysis + git archaeology, the workflow checks whether any loaded skill matches the issue query:

- **Search order**: `$CODEBASE_DIR/.claude/skills/` → `EXTRA_SKILLS_REPOS` paths → `~/.claude/skills/` → `~/.claude/skills/you-got-skills/skills/`
- **Matching**: keyword overlap (score ≥ 2) against each SKILL.md's `name`, `description`, `keywords` frontmatter, and first 600 chars of body
- **If matched**: the skill's SKILL.md instructions are prepended to the prompt and Claude is given full tool access (Bash, Read, Write, Edit) with `max_turns=20` — enabling active investigation (e.g., checking out the repo, running a flaky test N times, checking coverage)
- **If no match**: falls back to git archaeology + standard analysis

Task context keys emitted: `SKILL_USED::<name>` (when a skill is invoked), `GIT_ARCHAEOLOGY::yes|no`.

### Config variables

| Variable | CamelCase alias | Purpose |
|----------|-----------------|---------|
| `BITBUCKET_WORKSPACE` | `BitbucketWorkspace` | Bitbucket workspace/org |
| `BITBUCKET_REPO` | `BitbucketRepo` | Bitbucket repo name |
| `BITBUCKET_TOKEN` | `BitbucketToken` | ATATT token (HTTPS clone + REST API) |
| `GH_ORG` | `GitHubOrg` | GitHub organization |
| `GH_REPO` | `GitHubRepo` | GitHub repo name |
| `GH_TOKEN` | `GitHubToken` | GitHub personal access token |
| `EXTRA_SKILLS_REPOS` | `ExtraSkillsRepos` | Extra skill repos (JSON array or colon-separated paths) |

All CamelCase aliases match the Formicary org config property names — set them once in the org config and they apply to all workflows without any YAML changes.

### Graceful degradation

- No repo configured → plain analysis only (no git context)
- Clone fails → warning printed, analysis continues without git context
- Git commands time out or fail → empty string returned, analysis continues
- No skill match → standard analysis

---

## PR Review Workflow

The review pipeline runs Claude against a PR diff using the `ygs-review-pr` skill (or any other you-got-skills skill), posts findings as a Slack Block Kit message with **Approve** / **Request Changes** buttons, then pauses until a human clicks one.

### GitHub PR review

```bash
# Formicary job — pauses after posting findings, resumes on button click
formicary submit formicary/ai-gh-review.yaml \
  --var PRUrl=https://github.com/org/repo/pull/42 \
  --var SlackChannel=C123ABC

# Or via Slack — mention the bot:
# @ai-agent review https://github.com/org/repo/pull/42
```

### Jira/Bitbucket PR review

```bash
formicary submit formicary/ai-jira-review.yaml \
  --var PRUrl=https://bitbucket.org/workspace/repo/pull-requests/5 \
  --var SlackChannel=C123ABC
```

### What happens

1. `scripts.review.run` — Claude invokes `/ygs-review-pr` (or `/ygs-review-deep` when `ReviewDepth=deep`), writes `findings.json`
2. `scripts.review.post_findings` — reads findings, posts Block Kit to Slack, **exits 3 → PAUSE_JOB**
3. Human clicks Approve or Request Changes in Slack
4. Slack router resumes the job with `Decision=approve` or `Decision=request-changes`
5. `scripts.review.apply_feedback` — posts confirmation to the Slack thread

### Use a different skill

```bash
formicary submit formicary/ai-gh-review.yaml \
  --var PRUrl=https://github.com/org/repo/pull/42 \
  --var Skill=ygs-security-review   # override default ygs-review-pr
```

### Self-review in the implement pipeline

The implement workflow (`ai-gh-implement` / `ai-jira-implement`) runs a self-review pass automatically before creating the PR. After `implement`, `scripts.review.run` is called with `--mode self-review`; if it returns exit code 2 (BLOCKED), the job pauses for human review before the PR is created. This catches obvious mistakes before they hit the review queue.

### Complexity-tiered model selection

The `plan` step writes a `plan_complexity.txt` file (`low`, `medium`, or `high`). The `implement` step reads it and selects the model tier automatically:

| Complexity | Model |
|------------|-------|
| `low` | Claude Haiku (fast, cheap) |
| `medium` | Claude Sonnet (balanced) |
| `high` | Claude Opus (most capable) |

Override any step with `--var ModelId=...` when submitting the job.

---

## Slack Agent Router

The Slack router is a Bolt Socket Mode app (`scripts/slack/router.py`) that listens for bot mentions and routes them to formicary jobs. It requires no public ingress.

### Supported commands (mention the bot)

| Command | What it does | Workflow |
|---------|-------------|---------|
| `@bot help` | List all available commands and instructions for adding new skills | — |
| `@bot standup` | Compact daily brief: board status, per-person status, risks, discussion | `ai-standup-jira` / `ai-standup-gh` |
| `@bot risk` / `@bot risks` | Ranked sprint risks: stale work, PR bottlenecks, dependency chains | `ai-adhoc` (ygs-risk-scan) |
| `@bot prs` | Open PRs grouped by reviewer status, sorted by age | `ai-adhoc` |
| `@bot open prs` | Same as prs | `ai-adhoc` |
| `@bot review queue` | Same as prs | `ai-adhoc` |
| `@bot pr comments <url>` | All comments, inline feedback, and open tasks for a PR | `ai-adhoc` |
| `@bot pr feedback <url>` | Same as pr comments | `ai-adhoc` |
| `@bot review <github-pr-url>` | Full PR review: correctness, security, API, SRE | `ai-gh-review` |
| `@bot review <bitbucket-pr-url>` | Same for Bitbucket | `ai-jira-review` |
| `@bot security review <pr-url>` | OWASP-style security audit | `ai-gh-review` (ygs-security-review) |
| `@bot sre review <pr-url>` | Failure mode and operational risk review | `ai-gh-review` (ygs-sre-review) |
| `@bot deep review <pr-url>` | Seven-domain deep review (adds performance, testing quality, architecture) | `ai-gh-review` (`ReviewDepth=deep`) |
| `@bot implement PROJ-123` | Implement a Jira issue | `ai-jira-implement` |
| `@bot implement 42` | Implement a GitHub issue | `ai-gh-implement` |
| `@bot jira query <keywords>` | Search Jira issues by keyword | `ai-jira-query` |
| `@bot search jira <keywords>` | Same as jira query | `ai-jira-query` |
| `@bot jira-analyze PROJ-1,PROJ-2` | Analyze and summarize a set of Jira issues | `ai-jira-query` (Mode=analyze) |
| `@bot gh-query <keywords>` | Search GitHub issues by keyword | `ai-jira-query` (DefaultTracker=github) |
| `@bot gh-analyze #123,#456` | Analyze GitHub issues for root cause and fixes | `ai-jira-query` (Mode=analyze, DefaultTracker=github) |
| `@bot doctor` | Connectivity check against all configured services | `ai-connectivity-check` |

When a paused job is waiting in a thread, replying in that thread resumes the job with `ReplyText` set to your message.

### How routing works

1. **Slash-command parse** — deterministic match on the first word (`review`, `implement`, `standup`, `pr`, `risk`, `security`, `sre`)
2. **Haiku LLM fallback** — free-text classification when no verb matches
3. **`DEFAULT_TRACKER`** — when intent has no URL/issue key (e.g. bare `standup`), `DEFAULT_TRACKER` env var (`jira` or `github`) picks the right workflow. Default: `jira`. The router also auto-detects the tracker from URLs and keywords in the message — a Bitbucket URL always routes to Jira even if DEFAULT_TRACKER=github.
4. **Registry lookup** — `scripts/slack/workflows.yml` maps `(intent, target_kind)` → formicary `job_type`
5. **Submit** to formicary with `SlackThreadTs` stored as a job variable (formicary is the sole source of truth for paused state — no Redis needed)

### Extend the router

Add one entry to `scripts/slack/workflows.yml` to expose a new job type. Add one entry to `scripts/slack/skills.yml` to register a new skill. No code changes needed.

### Deploy

```bash
cd docs/examples
./deploy-ai-slack-router.sh --create-k8s-secret --set-configs \
  --slack-channel "$SLACK_CHANNEL" \
  --bot-name "@your-bot-name" \
  --default-tracker jira    # or "github"
```

Required secrets: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `FORMICARY_TOKEN`.
Key configs: `DEFAULT_TRACKER` — `jira` or `github`; `SLACK_BOT_NAME` — display name used in `@bot help` output.

See [docs/slack-setup.md](docs/slack-setup.md) for the full Slack app creation walkthrough (OAuth scopes, Socket Mode, Event Subscriptions, screenshots).

---

## Ad-hoc Skill Execution

Run any you-got-skills skill with a free-form prompt and get the result back in your Slack thread:

```bash
# Via Slack
@ai-agent standup
@ai-agent run a risk assessment for sprint 42

# Via formicary directly
formicary submit formicary/ai-adhoc.yaml \
  --var Skill=ygs-standup \
  --var Prompt="summarize open PRs for this week"
```

---

## Skills Management — EXTRA_SKILLS_REPOS

Load additional skill repositories at job startup without rebuilding the Docker image.
Set the `EXTRA_SKILLS_REPOS` org config (or env var) to a JSON array or a plain name:

```bash
# Plain bare name — auto-expands using DEFAULT_TRACKER + BITBUCKET_WORKSPACE / GH_ORG
EXTRA_SKILLS_REPOS=my-skills-repo

# Full URL
EXTRA_SKILLS_REPOS=https://github.com/org/agent-skills.git

# JSON array — full control
EXTRA_SKILLS_REPOS='[
  {
    "url": "https://bitbucket.org/myorg/myrepo.git",
    "skills_dir": "skills",    # auto-detected if omitted: .claude/skills → skills → .skills
    "sparse": true             # default true — sparse-checkout the skills_dir only
  },
  {
    "url": "https://github.com/vercel-labs/agent-skills",
    "type": "skills-cli"       # delegate to: npx skills add <repo> --agent claude-code
  }
]'
```

**Auto-credentials**: Bitbucket URLs use `BITBUCKET_TOKEN`/`BITBUCKET_USERNAME` automatically.
GitHub URLs use `GH_TOKEN`.  Override with explicit `token_env` / `username_env` in the entry.

**Sparse checkout** (default `true`): Downloads only the skills subdirectory — essential for
large monorepos.  Set `"sparse": false` for dedicated skills repos.

#### Task Context Variables

Every job emits the following `::add-task-context` keys, visible on the job request dashboard:

| Key | Description |
|-----|-------------|
| `SKILL` | Skill name passed to the task (e.g. `ygs-review-pr`) |
| `SKILL_LOADED` | `yes` if the skill's `SKILL.md` was found and loaded; `no` if fallback used |
| `YGS_SKILLS_COUNT` | Number of you-got-skills skills installed in the pod |
| `YGS_SKILLS_INSTALLED` | Comma-separated list of installed you-got-skills skill names |
| `YGS_SKILLS_REPO_URL` | Git URL of the you-got-skills base repo |
| `YGS_SKILLS_REPO_COMMIT` | Short commit hash of the cloned you-got-skills repo |
| `EXTRA_SKILLS_<SLUG>_COUNT` | Count of skills from each extra repo (one entry per repo) |
| `EXTRA_SKILLS_<SLUG>_INSTALLED` | Comma-separated skill names from each extra repo |
| `SKILLS_INVOKED` | Comma-separated list of skills Claude actually called during the session; `none` if none detected |
| `SELECTED_MODEL` | Resolved model ID used for the Claude invocation |

The `SKILLS_INVOKED` key is detected by scanning Claude's output for `/skill-name` patterns that
match installed skills. It tells you whether Claude actually used a skill during the session vs.
just having them available.

**`type: "skills-cli"`**: Delegates to the
[vercel-labs/skills](https://github.com/vercel-labs/skills) CLI (`npx skills add`),
which is compatible with [you-got-skills](https://github.com/bhatti/you-got-skills) SKILL.md
format.  Does not support sparse checkout.

**Process timeout**: Set `MAX_CLAUDE_PROCESS_TIMEOUT` (seconds) to kill the Claude process
if it exceeds that limit (prevents hitting the 25-minute Formicary task timeout):

```bash
MAX_CLAUDE_PROCESS_TIMEOUT=270   # 4.5 minutes
```

For full documentation see [docs/system-reference.md § 10b](docs/system-reference.md).

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

## Testing

There are two test layers:

| Layer | What | How to run |
|-------|------|------------|
| Unit tests | Python logic, mocked APIs | `make test` |
| Pod functional tests | Real scripts in real k8s pods | `make pod-test` |

### Unit and integration tests

```bash
# Install dev deps
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run all unit tests
make test            # runs pytest tests/ (excludes functional tests)

# Run with coverage
make test-cov

# Run a specific test module
python3 -m pytest tests/test_router.py -v
python3 -m pytest tests/test_formicary_client.py -v
python3 -m pytest tests/test_standup_gather_pr_queue.py -v
```

---

### Pod-based functional tests

`tests/test_pod_functional.py` simulates exactly what Formicary ant workers do —
without the full build/deploy cycle:

1. Creates a fresh k8s pod (`plexobject/ai-dev-tools:latest`)
2. Copies your **local** script files into the pod (overrides stale image layers)
3. Injects credentials from the `ai-dev-credentials` k8s secret
4. Runs each workflow step via `kubectl exec`
5. Parses `::add-task-context` markers from stdout
6. Deletes the pod (one pod per test, guaranteed cleanup)

**Prerequisites:** `kubectl` configured, `ai-dev-credentials` secret deployed,
`~/.zshrc` exporting `ANTHROPIC_BEDROCK_BASE_URL` (or `ANTHROPIC_API_KEY`).

```bash
source ~/.zshrc

# Run default tests (jira-query + jira-analyze + standup-gather):
make pod-test

# Run all tests (requires ISSUE_ID for jira-analyze):
ISSUE_ID=PROJ-123 make pod-test-all

# Run a specific test:
python3 tests/test_pod_functional.py --tests jira-query
python3 tests/test_pod_functional.py --tests standup-gather
python3 tests/test_pod_functional.py --tests standup-pipeline   # gather → synthesize

# Run the full analyze test with a real Jira issue:
ISSUE_ID=PROJ-123 python3 tests/test_pod_functional.py --tests jira-analyze

# List all available pod tests:
python3 tests/test_pod_functional.py --list
```

**Available pod tests:**

| Test | What it verifies |
|------|-----------------|
| `jira-query` | Jira connectivity, context markers `SELECTED_TRACKER`, `ISSUE_COUNT` |
| `jira-analyze` | Claude analysis of a Jira issue, `ANALYSIS_TYPE` key (requires `ISSUE_ID`) |
| `standup-gather` | `gather_jira` step — Jira sprint data, `signals.json` written |
| `standup-pipeline` | Full pipeline: gather → synthesize in one pod (shared workspace) |
| `gh-query` | GitHub issue query, `SELECTED_TRACKER=github` |
| `gh-analyze` | Claude analysis of GitHub issues, `ANALYSIS_TYPE` key |

Each test creates its own pod, copies scripts, runs, then deletes the pod — clean isolation.
The standup-pipeline test runs `gather_jira` then `synthesize` in the **same pod** so
`synthesize` can read the `signals.json` written by `gather`, matching the real Formicary flow.

```bash
# Skip copying local scripts (use image as-is):
SKIP_COPY=1 python3 tests/test_pod_functional.py --tests jira-query
```

---

### Test Slack router locally (no Kubernetes)

Use the interactive Slack REPL — it simulates the full @bot mention flow without a real Slack workspace:

```bash
source ~/.zshrc   # loads FORMICARY_URL, FORMICARY_TOKEN, SLACK_CHANNEL, DEFAULT_TRACKER
cd /path/to/ai-dev-tools

# Live mode (default) — submits real jobs to your Formicary server:
python3 scripts/slack/slack_repl.py

# Dry-run mode — prints what would be submitted, no network calls:
python3 scripts/slack/slack_repl.py --dry-run

# Override channel and tracker:
python3 scripts/slack/slack_repl.py --channel C0123ABC --tracker github
```

Once inside the REPL, type commands exactly as you would in Slack (without the @mention):

```
you> standup
you> prs
you> risk
you> review https://github.com/org/repo/pull/42
you> security review https://github.com/org/repo/pull/42
you> jira query open authentication bugs
you> implement PROJ-123
you> /workflows       ← list all loaded workflows
you> /skills          ← list all skills
you> /mode            ← show live vs dry-run
you> /tracker github  ← switch DEFAULT_TRACKER mid-session
you> /thread approve  ← simulate a thread reply to the last job
```

**Tab-completion** is built in — press Tab to complete trigger words and REPL commands.

**Why `source ~/.zshrc` is required before live mode:** the REPL builds the Formicary client from the current shell env. If `FORMICARY_URL` is not exported, it falls back to `localhost:7777` and fails with connection errors. The REPL prints a clear error and exits if the required vars are missing.

See [`docs/slack-router.md`](docs/slack-router.md#testing-locally) for the full REPL reference.

---

**Low-level one-liners** (when you need to call a specific Formicary API directly without the router):

```bash
# Trigger the standup cron slot
python3 -c "
import os, sys
sys.path.insert(0, '.')
from scripts.slack.formicary_client import FormicaryClient
c = FormicaryClient(base_url=os.environ['FORMICARY_URL'], token=os.environ['FORMICARY_TOKEN'])
result = c.trigger_pending_or_submit('ai-standup-jira', {
    'SlackChannel': os.environ.get('SLACK_CHANNEL', ''),
    'SlackThreadTs': 'local-test.000',
})
print('standup result:', result)
# id → triggered; _already_executing → already running; _no_cron_slot → redeploy
"
```

---

### Test standup gather locally

```bash
source ~/.zshrc

JIRA_BASE_URL="$JIRA_BASE_URL" \
JIRA_EMAIL="$JIRA_EMAIL" \
JIRA_API_TOKEN="$JIRA_API_TOKEN" \
JIRA_PROJECT="$JIRA_PROJECT" \
BITBUCKET_WORKSPACE="$BITBUCKET_WORKSPACE" \
BITBUCKET_REPO="$BITBUCKET_REPO" \
BITBUCKET_USERNAME="$BITBUCKET_USERNAME" \
BITBUCKET_TOKEN="$BITBUCKET_TOKEN" \
WORKSPACE_DIR="/tmp/standup_test" \
python3 -m scripts.standup.gather_jira

cat /tmp/standup_test/signals.json | python3 -m json.tool | head -60
```

---

### Test PR queue gather locally

```bash
source ~/.zshrc

JIRA_BASE_URL="$JIRA_BASE_URL" \
JIRA_EMAIL="$JIRA_EMAIL" \
JIRA_API_TOKEN="$JIRA_API_TOKEN" \
JIRA_PROJECT="$JIRA_PROJECT" \
BITBUCKET_WORKSPACE="$BITBUCKET_WORKSPACE" \
BITBUCKET_REPO="$BITBUCKET_REPO" \
BITBUCKET_USERNAME="$BITBUCKET_USERNAME" \
BITBUCKET_TOKEN="$BITBUCKET_TOKEN" \
DEFAULT_TRACKER="jira" \
WORKSPACE_DIR="/tmp/pr_queue_test" \
python3 -m scripts.standup.gather_pr_queue

# Should show: "N open PRs linked to sprint issues"
cat /tmp/pr_queue_test/pr_queue.json | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'sprint: {d[\"sprint\"]}  pr_count: {d[\"pr_count\"]}')
for pr in d['prs'][:5]:
    print(f'  [{pr[\"jira_key\"]}] PR {pr[\"id\"]}: {pr[\"title\"][:50]}')
    print(f'    approved_by={pr[\"approved_by\"]}  pending={pr[\"reviewers\"][:2]}')
    print(f'    url={pr[\"url\"]}')
"
```

---

### Test ad-hoc skill locally

```bash
source ~/.zshrc

# Run the ygs-pr-queue skill with the gathered pr_queue.json
WORKSPACE_DIR="/tmp/pr_queue_test" \
SLACK_CHANNEL="${SLACK_CHANNEL:-}" \
SLACK_THREAD_TS="" \
SKILL_NAME="ygs-pr-queue" \
SKILL_PROMPT="" \
python3 -m scripts.adhoc.run_skill --skill ygs-pr-queue --prompt ""
```

---

### Test via Slack (requires deployed router)

After running `deploy-ai-slack-router.sh`, mention the bot in your channel:

| Command | What to verify |
|---|---|
| `@bot standup` | Standup brief posted to thread; job appears in Formicary UI |
| `@bot prs` | PR table posted: Jira key, PR number, description, status, reviewers |
| `@bot risk` / `@bot risks` | Risk list posted |
| `@bot review <pr-url>` | Review findings Block Kit posted with Approve / Request Changes buttons |

**Check router logs for errors:**
```bash
kubectl logs -l app=ai-slack-router --tail=50 -f
```

**Verify job was created:**
```bash
source ~/.zshrc
curl -s "$FORMICARY_URL/api/v1/jobs/requests?pageSize=5" \
  -H "Authorization: Bearer $FORMICARY_TOKEN" | python3 -c "
import json,sys
d=json.load(sys.stdin)
items = d if isinstance(d,list) else (d.get('records') or d.get('Records') or [])
for j in items[:5]:
    print(f'{j[\"id\"]:26s} {j[\"job_type\"]:30s} {j[\"job_state\"]}')
"
```

---

## Development

```bash
# Install dev deps
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run tests (240 tests)
make test

# Run with coverage
make test-cov

# Clean build artifacts
make clean
```

## License

MIT — see [LICENSE](LICENSE).
