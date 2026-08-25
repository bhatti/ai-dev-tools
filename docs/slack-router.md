# Slack Router — How It Works

The Slack router (`scripts/slack/router.py`) is a long-running
[Slack Bolt](https://slack.dev/bolt-python/) Socket Mode app deployed as a
single-replica Kubernetes Deployment.  It bridges Slack mentions to Formicary
jobs and handles job output back into the originating Slack thread.

---

## High-Level Flow

```
User types in Slack                    Formicary cluster
──────────────────                     ──────────────────
@bot implement PROJ-42
        │
        ▼
  router.py receives event
        │
        ├─ 1. strip @bot mention
        ├─ 2. normalise Slack links  ←  <url|PROJ-NNN> → PROJ-NNN
        ├─ 3. verb parse  (cheap, no LLM)
        │       or Haiku LLM classify (fallback)
        ├─ 4. registry lookup → job_type = "ai-jira-implement"
        ├─ 5. build params:
        │       SlackChannel   = event["channel"]  ← always auto-derived
        │       SlackThreadTs  = message ts
        │       IssueNumber    = "PROJ-42"
        │       UserTag        = "alice"            ← routes to user's ant
        └─ 6. POST /api/jobs/requests → formicary
                        │
                        ▼
              Formicary runs job on alice's ant worker
              (pod_labels: user=alice)
                        │
                        ▼
              notify(config, text)
              reads SlackThreadTs from env
              POSTs chat.postMessage with thread_ts
                        │
                        ▼
              Reply appears in original thread ◄─────────────
```

---

## Step-by-Step: What Happens on a Mention

### 1. Event receipt (Socket Mode)
The router holds an outbound WebSocket to Slack — no public ingress is needed.
Slack sends `app_mention` and `message` events over this socket.

### 2. Bot mention stripped
`<@UXXXXX>` is removed from the start of the text.

### 3. Slack link normalisation
Slack auto-links Jira keys as `<https://org.atlassian.net/browse/PROJ-42|PROJ-42>`.
The router extracts the bare Jira key so routing works regardless of how Slack formats the text.

### 4. Intent resolution (two-stage)

**Stage A — verb parse (fast, deterministic)**

The first word is matched against a table of verb aliases:

| User types | Resolved intent |
|------------|----------------|
| `review <url>` | `review` |
| `implement PROJ-42` | `implement` |
| `standup` / `status` / `daily` | `standup` |
| `risk` / `risks` | `risk scan` |
| `prs` / `queue` / `pulls` | `pr queue` |
| `security <rest>` | `security review` (qualifier word stripped from `rest` before URL extraction) |
| `sre <rest>` | `sre review` (qualifier word stripped from `rest` before URL extraction) |
| `jira <rest>` | sub-intent dispatch: `jira query <text>` → Query=text; `jira analyze <keys>` → Mode=analyze |
| `search` / `find` | `jira query` |
| `analyze` / `analyse` | `jira-analyze` |
| `gh query <text>` / `gh-query <text>` | `"gh query"` (target_kind=github) |
| `gh-analyze <text>` | `"gh-analyze"` (target_kind=github) |
| `doctor` / `health` | `doctor` |
| `help` / `?` | `__help__` |

**Stage B — Haiku LLM classify (fallback)**

If the verb isn't recognised, a `claude --model haiku --max-turns 1` subprocess
is called with the full message text.  It returns structured JSON:
```json
{"intent": "review", "target_kind": "github", "entity_id": "https://github.com/..."}
```

### 5. Registry lookup

`workflows.yml` maps `(intent, target_kind)` → a `WorkflowEntry`.
Resolution priority:
1. Exact `target_kind` match
2. `DEFAULT_TRACKER` match when target is ambiguous — set via env var or `ai-dev-tools/.env` file (`DEFAULT_TRACKER=jira`)
3. `target_kind: any` entry
4. First match (fallback)

`DEFAULT_TRACKER` (`jira` or `github`) routes bare commands like `standup` to the right workflow when no URL is present. Set in shell (`export DEFAULT_TRACKER=jira`), `.env` file, or org config.

**Smart override**: Even when `DEFAULT_TRACKER` is set, the router scans the full message text for tracker signals and overrides the default:
- `bitbucket.org`, `atlassian.net`, or the word `jira`/`bitbucket` → forces `jira`
- `github.com` or the word `github` → forces `github`
- `jira-query`, `jira query` verbs → always force `jira` regardless of `DEFAULT_TRACKER`
- `gh-query`, `gh query` verbs → always force `github` regardless of `DEFAULT_TRACKER`

`target_kind` is inferred from the entity ID:
- Jira key `PROJ-42` → `jira`
- GitHub PR URL → `github`
- Bitbucket PR URL → `jira` (routes to Jira-side review workflow)
- Bare text → `any`

### 6. Param assembly

```python
params = {
    "SlackChannel":  event["channel"],   # always auto-derived from the event
    "SlackThreadTs": message_ts,         # enables thread reply
    "UserTag":       "alice",            # routes pod to alice's ant worker
    **entry.extra_params,                # static overrides from workflows.yml
    entry.id_var: entity_id,             # e.g. IssueNumber="PROJ-42"
    "Prompt": resolved_prompt,
}
```

**Channel auto-derive**: `event["channel"]` is always the Slack channel ID where
the user typed.  This means replies always land in the correct channel without any
static configuration.  The `SlackChannel` org config is only a fallback for
contexts where there is no event (e.g. cron jobs).

**UserTag**: read from `USER_TAG` org config (set per-user during deploy).  Routes
job pods to the user's personal ant worker via `pod_labels: {user: <UserTag>}`.
Leave empty to run on any available ant worker.

### 7. Job submission

```
POST /api/jobs/requests
{job_type: "ai-jira-implement", params: {...}}
```

The router immediately replies to the thread:
> `Started ai-jira-implement (job abc123) — I'll post updates here. [View job]`

---

## Thread Replies — How Skills Reply In-Thread

Every job carries `SlackThreadTs` as a job variable.  Formicary injects job
variables as environment variables in the container.  The skill script reads it:

```python
# scripts/standup/slack_client.py
thread_ts = config.get("SlackThreadTs") or config.get("SLACK_THREAD_TS") or None
post_message(config, text, thread_ts=thread_ts, blocks=blocks)
```

`chat.postMessage` with `thread_ts` set posts **into the thread** where the user
originally typed the command.

---

## Thread Replies from Users — Paused Job Resume

When the router receives a `message` event **inside an existing thread**, it checks
whether a Formicary job is paused on that thread:

```python
jobs = client.find_jobs(state="PAUSED", var_filter={"SlackThreadTs": thread_ts})
if jobs:
    client.resume(job_id, variables={"ReplyText": user_text})
```

The resume uses a 4-step pattern (because Formicary's trigger endpoint doesn't
accept a request body):
1. `GET /api/jobs/requests/{id}` — fetch current params
2. Merge `ReplyText` / `Decision` into params
3. `PUT /api/jobs/requests/{id}` — write updated params
4. `POST /api/jobs/requests/{id}/trigger` — signal resume

If no paused job is found on that thread, the message is treated as a new request.

---

## Extending Workflows (no image rebuild)

There are three ways to add new Slack commands, in order of least to most invasive:

### Option A — Custom workflow via org config (no PR required)

Push a YAML blob to the `ExtraWorkflows` Formicary org config.  The router merges
it at startup — custom entries take precedence over built-in ones on trigger collision.

```bash
# 1. Create my-workflows.yml
cat > my-workflows.yml <<'EOF'
workflows:
  - name: my-deploy
    job_type: ai-adhoc          # reuse the adhoc job type
    shape: simple
    triggers: ["deploy", "release", "ship it"]
    skill: ygs-deploy           # skill in you-got-skills or codebase-local
    id_var: ""
    required_vars: []
    target_kind: any
    description: "Trigger a deployment workflow"
EOF

# 2. Push it to Formicary org config
python -m scripts.slack.deploy_workflows \
  --server http://localhost:7777 \
  --set-extra-workflows my-workflows.yml

# 3. Restart the router to pick it up
kubectl rollout restart deployment/ai-slack-router
```

The YAML format is identical to `scripts/slack/workflows.yml`.  Custom entries are
prepended so they win if a trigger matches both a custom and a built-in entry.

To remove custom workflows, pass an empty YAML or delete the `ExtraWorkflows` org
config via the Formicary UI.

### Option B — Edit workflows.yml in the repo (PR, image rebuild)

Add an entry to `scripts/slack/workflows.yml`.  This is the right choice for
platform-wide features that everyone should have.

```yaml
- name: my-pipeline
  job_type: ai-my-pipeline      # must match a YAML in docs/examples/
  triggers: ["my command"]
  id_var: IssueNumber
  required_vars: [IssueNumber]
  target_kind: jira
  description: "What this does"
```

Then add `docs/examples/ai-my-pipeline.yaml` and run `deploy-ai-jira-workflows.sh`.
Rebuild the image and redeploy.

### Option C — Extra params on existing job type

Reuse an existing job with static params injected at submission time:

```yaml
- name: security-review
  job_type: ai-gh-review        # same job as regular review
  triggers: ["security review", "security"]
  id_var: PRUrl
  required_vars: [PRUrl]
  target_kind: github
  extra_params:
    Skill: ygs-security-review  # override which skill to run
  description: "Security-focused PR review"
```

---

## Adding Codebase-Local Skills

Skills in `.claude/skills/<name>/SKILL.md` inside your project repo take priority
over global you-got-skills skills.  No changes to the router required.

**How it works:**

When the implement/review workflow clones your repo to `/workspace/repo`, it sets
`CODEBASE_DIR=/workspace/repo`.  The skill runner checks this path first:

```
/workspace/repo/.claude/skills/ygs-review-pr/SKILL.md   ← codebase-local (wins)
/workspace/you-got-skills/skills/ygs-review-pr/SKILL.md  ← global fallback
```

**To add a codebase-local skill:**

1. Create `.claude/skills/<skill-name>/SKILL.md` in your project repo
2. The skill will be used automatically on the next run — no config change needed

For local dev (running `claude` CLI directly without a container), install the global
skills library:

```bash
cd ~/workplace/ai-dev-tools
make install-skills
# → clones you-got-skills into ~/.skills/you-got-skills
```

---

## Changing the AI Model

Models are configured as Formicary org configs, not env vars.  This lets you change
models without redeploying the image.

```bash
# Change the main implementation model (Sonnet)
python -m scripts.slack.deploy_workflows \
  --server http://localhost:7777 \
  --set-config AnthropicSonnetModel us.anthropic.claude-sonnet-5

# Change the planner model (Opus)
python -m scripts.slack.deploy_workflows \
  --server http://localhost:7777 \
  --set-config AnthropicOpusModel us.anthropic.claude-opus-5

# Change the intent-classification model (Haiku — used by the router)
python -m scripts.slack.deploy_workflows \
  --server http://localhost:7777 \
  --set-config AnthropicHaikuModel claude-haiku-4-5-20251001
```

Or set them in one call via `deploy-ai-workflows.sh --set-configs` (see that script's `--help`).

The values flow as:
1. Org config `AnthropicSonnetModel` → Formicary job variable `AnthropicSonnetModel`
2. Injected as `ANTHROPIC_DEFAULT_SONNET_MODEL` env var in each container
3. `config.get("AI_MODEL")` → `scripts/common/config.py` → passed to `claude --model`

For the router's Haiku classifier specifically, the env var is `ANTHROPIC_DEFAULT_HAIKU_MODEL`
(read from org config `AnthropicHaikuModel`).

---

## Multi-User Infra — Per-User Ant Workers

When multiple team members share one Formicary leader, each person runs a personal
ant worker tagged with their user ID.  Jobs submitted by that person route exclusively
to their ant.

### How routing works

Every AI workflow YAML contains:

```yaml
job_variables:
  UserTag: ""          # empty = any ant; set to "alice" to pin to alice's ant

tasks:
  - task_type: implement
    pod_labels:
      user: "{{.UserTag}}"
```

When the router submits a job, it injects `UserTag` from the `USER_TAG` org config.
Formicary's scheduler matches `pod_labels` against ant worker labels — only ants
registered with `pod_labels: {user: alice}` pick up jobs tagged `user=alice`.

### Setting up your personal ant worker

See `formicary/docs/ant-worker-setup.md` for the full onboarding guide.  The short version:

```bash
cd ~/workplace/formicary
./scripts/setup-ant-worker.sh --queen <ec2-host> --token $FORMICARY_TOKEN
```

### Setting your UserTag

```bash
# Sets UserTag org config so the router always routes your jobs to your ant
./docs/examples/deploy-ai-workflows.sh --set-configs --ant-user-tag "${USER}"
```

---

## `@bot doctor` — Connectivity Check

`@bot doctor` (or `@bot health check`, `@bot check connectivity`) runs the
`ai-connectivity-check` job, which validates all credentials and service
connectivity and posts results to your Slack thread.

```
@bot doctor
→ Runs ai-connectivity-check job
→ Tests: Jira API, GitHub API, Bitbucket API, Bedrock/Anthropic, Slack
→ Posts pass/fail results in thread
```

---

## Configuration Reference

| Org config key | What it controls | Example |
|----------------|-----------------|---------|
| `DefaultTracker` | Jira or GitHub when intent is ambiguous | `jira` |
| `SlackChannel` | Fallback channel for cron/non-event jobs | `C0123ABCD` |
| `SlackBotName` | Display name in `@bot help` examples | `@mybot` |
| `FormicaryUrl` | Internal Formicary URL (for job links) | `http://formicary:7777` |
| `FormicaryPublicUrl` | Public URL for clickable job links | `http://host:7777` |
| `UserTag` | Route jobs to user's ant worker | `alice` |
| `AnthropicSonnetModel` | Model for implement/review tasks | `us.anthropic.claude-sonnet-4-6` |
| `AnthropicOpusModel` | Model for planning tasks | `us.anthropic.claude-opus-4-6-v1` |
| `AnthropicHaikuModel` | Model for intent classification | `claude-haiku-4-5-20251001-v1:0` |
| `ClaudeUseBedrock` | Use AWS Bedrock proxy | `1` |
| `AnthropicBedrockBaseUrl` | Bedrock proxy URL | `http://ai/bedrock` |
| `ExtraWorkflows` | YAML blob of custom workflow entries | see above |
| `ExtraSkills` | YAML blob of custom skill entries | see above |

---

## Available Commands (current)

| Command | Example | What it does |
|---------|---------|-------------|
| `standup` | `@bot standup` | Daily standup brief from Jira or GitHub |
| `risk` / `risks` | `@bot risk` | Sprint risk scan — stale, blocked, capacity |
| `prs` / `pr queue` | `@bot prs` | Open PR queue grouped by reviewer |
| `review <url>` | `@bot review https://github.com/...` | AI code review → Slack thread, pause for decision |
| `implement <id>` | `@bot implement PROJ-42` | Full implement pipeline — plan → code → PR |
| `jira query <keywords>` / `search jira <keywords>` | `@bot jira query flaky tests` | Search open Jira/GitHub issues by keyword |
| `jira-analyze <keys>` / `analyze issues <keys>` | `@bot jira-analyze PROJ-1,PROJ-2` | Root-cause + fix analysis for issues (Mode=analyze) |
| `gh-query <text>` | `@bot gh-query open issues` | Search open GitHub issues by keyword |
| `gh-analyze <text>` | `@bot gh-analyze #123,#456` | Analyze GitHub issues for root cause and fixes |
| `pr comments <url>` | `@bot pr comments <url>` | Inline comments + open tasks for a PR |
| `security review <url>` | `@bot security review <url>` | Security-focused PR review |
| `sre review <url>` | `@bot sre review <url>` | SRE/reliability-focused PR review |
| `doctor` | `@bot doctor` | Validate all credentials and connectivity |
| `help` | `@bot help` | List all commands |

---

## Testing Locally

### `scripts/slack/slack_repl.py` — Interactive REPL

`slack_repl.py` is the primary tool for testing the router without a real Slack
workspace.  It simulates the full `@bot` mention flow — verb parse, registry
lookup, param assembly, and job submission — entirely on your machine.

**Live vs dry-run mode**

| Mode | What happens |
|------|-------------|
| Live (default) | Submits real jobs to Formicary via API; requires `FORMICARY_URL` and `FORMICARY_TOKEN` |
| `--dry-run` | No network calls; prints what would be submitted in a formatted output box |

**How to run**

```bash
# Dry-run — no Formicary needed:
python3 scripts/slack/slack_repl.py --dry-run

# Live — submits real jobs:
export FORMICARY_URL=https://your-formicary-host
export FORMICARY_TOKEN=your-admin-token
export FORMICARY_TLS_VERIFY=false   # if self-signed cert
python3 scripts/slack/slack_repl.py

# Override tracker and channel:
python3 scripts/slack/slack_repl.py --dry-run --tracker github --channel C0123ABC
```

Type commands at the `you>` prompt exactly as you would in Slack (no `@mention`
prefix needed).  Tab-completion is available for both REPL commands and Slack verbs.

**REPL special commands** (not forwarded to Formicary)

| Command | What it does |
|---------|-------------|
| `/workflows` | List all loaded workflows with job_type and triggers |
| `/skills` | List all loaded skills |
| `/mode` | Show current live/dry-run mode and Formicary URL |
| `/thread <text>` | Simulate a thread reply to the last submitted job |
| `/verbose` | Toggle verbose output (shows thread_ts and raw routing params) |
| `/tracker <jira\|github>` | Switch `DEFAULT_TRACKER` mid-session |
| `quit` / `exit` / `q` | Exit the REPL |

**Unit tests**

Routing logic is covered in `tests/test_router.py`.  Run with:

```bash
python -m pytest tests/test_router.py -v
```

**Example session**

```
╔══════════════════════════════════════════════════════════════════════╗
║                        Slack Router REPL                             ║
╠══════════════════════════════════════════════════════════════════════╣
║  Mode    : DRY-RUN (no Formicary calls)                              ║
║  Channel : C_REPL                                                    ║
║  Tracker : jira                                                      ║
║  Bot     : @bot                                                      ║
╚══════════════════════════════════════════════════════════════════════╝

you> jira query flaky tests

┌── DRY-RUN SUBMIT ──────────────────────────────────────────────────
│  job_type : ai-jira-query
│  IssueQuery             = flaky tests
│  SlackChannel           = C_REPL
│  SlackThreadTs          = 1000001.000
└────────────────────────────────────────────────────────────────────

┌── BOT REPLY ───────────────────────────────────────────────────────
│  Started ai-jira-query (job dry-run-job-001) — I'll post updates here.
└────────────────────────────────────────────────────────────────────

you> security review https://github.com/org/repo/pull/42

┌── DRY-RUN SUBMIT ──────────────────────────────────────────────────
│  job_type : ai-gh-review
│  PRUrl                  = https://github.com/org/repo/pull/42
│  Skill                  = ygs-security-review
│  SlackChannel           = C_REPL
│  SlackThreadTs          = 1000002.000
└────────────────────────────────────────────────────────────────────

you> /workflows

┌── WORKFLOWS (14) ──────────────────────────────────────────────────
│  gh-implement           job=ai-gh-implement
│    triggers: implement, build, gh issue
│  jira-implement         job=ai-jira-implement
│    triggers: implement, build, jira issue
│  ...
└────────────────────────────────────────────────────────────────────

you> quit
[repl] bye
```

---

## See Also

- [Architecture](architecture.md) — overall system design
- [Configuration Reference](configuration.md) — all env vars
- [Slack App Setup](slack-setup.md) — create the Slack app, OAuth scopes, Socket Mode
- [Kubernetes Deployment](k8s-deployment.md) — deploy the router pod
- [System Reference](system-reference.md) — complete operational reference
- `formicary/docs/ant-worker-setup.md` — per-user ant worker onboarding
