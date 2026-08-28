# System Reference — Formicary + ai-dev-tools + you-got-skills

> **Purpose of this document**: Complete operational and architectural reference for
> future AI sessions (and human engineers) working in this codebase.  Read this
> before touching anything.  It captures what is deployed, how every piece fits
> together, and all the hard-won gotchas found during development.

---

## 1. Three-Layer Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│ LAYER 1 — INTERFACE (Slack, built into Formicary queen)            │
│   User @mentions → Formicary SlackService (Socket Mode, xapp- tok) │
│   Routing: verb → job_type from SlackRoutes admin config (server)  │
│   Per-user identity: Slack UID → Formicary user (one-time DM setup)│
│   Job scoped to user's OrgID — multi-tenant isolation enforced     │
│   Results posted back to originating thread via SlackThreadTs      │
│                                                                    │
│   NO SEPARATE ROUTER POD — routing is server-side in the queen.   │
│   scripts/slack/router.py and registry.py have been REMOVED.      │
│   Clients only need ant workers; route table lives in Formicary.   │
└────────────────────────────────────────────────────────────────────┘
                              │ internal SaveJobRequest(qc, req)
┌────────────────────────────────────────────────────────────────────┐
│ LAYER 2 — ORCHESTRATION (Formicary)                                │
│   YAML job definitions in docs/examples/ai-*.yaml                  │
│   Runs tasks as Kubernetes pods (method: KUBERNETES)               │
│   Handles: cron, retry, dependency chaining, pause/resume          │
│   Config stored as org configs (non-secret) + K8s secret (secret) │
└────────────────────────────────────────────────────────────────────┘
                              │ kubectl pod
┌────────────────────────────────────────────────────────────────────┐
│ LAYER 3 — EXECUTION (ai-dev-tools Docker image)                    │
│   plexobject/ai-dev-tools:latest                                   │
│   Python scripts in scripts/{gh,jira,standup,adhoc,slack,review}/  │
│   Tools available: claude CLI, gh CLI, acli, git, python3          │
│   Skills loaded from ~/.claude/skills/you-got-skills/skills/       │
└────────────────────────────────────────────────────────────────────┘
```

---

## 2. Deployed Formicary Job Types

| Job type | Trigger | What it does |
|----------|---------|-------------|
| `ai-standup-jira` | Cron 8am weekdays + `@bot standup` | Gathers Jira sprint + Bitbucket PRs + Slack → Claude `ygs-standup` → post brief |
| `ai-standup-gh` | Cron 8am weekdays + `@bot standup` | Same for GitHub |
| `ai-jira-implement` | `@bot implement PROJ-NNN` | plan → implement → create PR → poll PR (Jira/Bitbucket) |
| `ai-gh-implement` | `@bot implement 42` | Same for GitHub |
| `ai-jira-review` | `@bot review <bitbucket-url>`, `@bot deep review` | Review Bitbucket PR; `ReviewDepth=deep` adds performance/testing/architecture passes |
| `ai-gh-review` | `@bot review <github-url>`, `@bot deep review` | Review GitHub PR; `ReviewDepth=deep` adds performance/testing/architecture passes |
| `ai-jira-issue-picker` | Cron `*/5 * * * *` | Picks Jira issues labeled `ai-ready`, launches implement pipeline |
| `ai-adhoc` | `@bot risk`, `@bot prs`, `@bot pr comments` | Run any you-got-skills skill, post to Slack thread |
| `ai-jira-query` | `@bot jira-query <term>`, `@bot jira-analyze`, `@bot gh-query <term>`, `@bot gh-analyze` | Search Jira or GitHub issues (DEFAULT_TRACKER controls backend); `Mode=analyze` for root cause analysis |
| `ai-connectivity-check` | Manual | Verify Jira/Bitbucket/Slack credentials are reachable |

All YAML definitions live in:
```
~/workplace/formicary/docs/examples/ai-*.yaml
```

Deploy/update all at once:
```bash
cd ~/workplace/formicary/docs/examples
bash deploy-ai-jira-workflows.sh
```

---

## 3. Configuration: Two Stores, One Secret

### 3.1 Kubernetes Secret — `ai-dev-credentials`

Holds all secrets.  Created/updated by the deploy script with `--create-k8s-secret`.
Mounted as `env_from: secret_ref: ai-dev-credentials` in every K8s task.

| Key | Value |
|-----|-------|
| `JIRA_BASE_URL` | `https://yourorg.atlassian.net` |
| `JIRA_EMAIL` | Atlassian account email |
| `JIRA_API_TOKEN` | Jira API token (from id.atlassian.com) |
| `JIRA_HOST` | `yourorg.atlassian.net` (no scheme) |
| `BITBUCKET_WORKSPACE` | Bitbucket workspace slug |
| `BITBUCKET_USERNAME` | Bitbucket username (not email) |
| `BITBUCKET_TOKEN` | Bitbucket app password (`ATATT...`) |
| `GH_TOKEN` | GitHub PAT with `repo` scope |
| `GH_ORG` | GitHub org |
| `GH_REPO` | GitHub repo |
| `SLACK_BOT_TOKEN` | `xoxb-...` bot OAuth token |
| `ANTHROPIC_API_KEY` | Direct Anthropic API key (not needed with Bedrock) |
| `SSH_PRIVATE_KEY` | PEM SSH private key for git clone |

Recreate the secret (updates in place, safe to re-run):
```bash
export JIRA_API_TOKEN=... BITBUCKET_TOKEN=... SLACK_BOT_TOKEN=...
bash deploy-ai-jira-workflows.sh --create-k8s-secret
```

### 3.2 Formicary Org Configs — non-secret shared settings

Stored in Formicary's database under the org.  Injected into jobs as template
variables (e.g. `{{.JiraProject}}`).  Set via:
```bash
bash deploy-ai-jira-workflows.sh --set-configs \
  --jira-project PROJ --bb-workspace myworkspace --bb-repo myrepo \
  --slack-channel my-channel --bedrock
```

| Org config key | Template variable | Description |
|----------------|------------------|-------------|
| `JiraUrl` | `{{.JiraUrl}}` | `https://yourorg.atlassian.net` |
| `JiraProject` | `{{.JiraProject}}` | Jira project key |
| `BitbucketWorkspace` | `{{.BitbucketWorkspace}}` | Bitbucket workspace slug |
| `BitbucketRepo` | `{{.BitbucketRepo}}` | Default repo |
| `DefaultTracker` | `{{.DefaultTracker}}` | `jira` or `github` |
| `SlackChannel` | `{{.SlackChannel}}` | Default Slack channel for posts |
| `ClaudeUseBedrock` | `{{.ClaudeUseBedrock}}` | `1` = use AWS Bedrock |
| `AnthropicBedrockBaseUrl` | `{{.AnthropicBedrockBaseUrl}}` | Bedrock proxy URL |
| `ClaudeSkipBedrockAuth` | `{{.ClaudeSkipBedrockAuth}}` | `1` = skip auth (internal proxy) |
| `GitUserName` | `{{.GitUserName}}` | Git commit author name |
| `GitUserEmail` | `{{.GitUserEmail}}` | Git commit author email |
| `FormicaryPublicURL` | `{{.FormicaryPublicURL}}` | Public URL for clickable job links in Slack |
| `StandupTeamMembers` | `{{.StandupTeamMembers}}` | Comma-separated display names to restrict standup output (optional — auto-derived from sprint board when empty) |
| `JiraBoards` | `{{.JiraBoards}}` | Board ID fast path for standup (optional — auto-discovered from sprint membership when empty) |

### 3.3 How env vars reach scripts

```
K8s Secret (ai-dev-credentials)
    └─ env_from: secret_ref  →  env in pod
Org configs
    └─ YAML template: {{.JiraUrl}}  →  JIRA_BASE_URL env in pod
scripts/common/config.py load_config()
    └─ reads os.environ  →  config dict  →  every script
```

`load_config()` also resolves short aliases: `BB_REPO → BITBUCKET_REPO`,
`GITHUB_TOKEN → GH_TOKEN`, etc.

---

## 4. Slack Routing — How a Message Becomes a Job

Routing is **server-side in the Formicary queen** (`queen/slack/`).
There is no separate router pod. `scripts/slack/router.py` and `registry.py`
have been removed.

```
@bot jira-query flaky
     │
     1. Formicary SlackService receives Socket Mode event (xapp- token)
     2. UserRegistry.LookupBySlackID(evt.User)
        → finds registered Formicary user + org_id
        → returns nil if user has not DM'd "setup <token>" yet
     3. CommandRouter.Route(text)
        → strips <@UXXXXX> mention prefix
        → matches first word against SlackRoutes trigger table
        → returns {JobType, Trailing, IdVar, Params}
     4. Builds job request:
           SlackChannel   = channel_id
           SlackThreadTs  = message.ts   ← HOW THREAD REPLIES WORK
           SlackUserId    = evt.User
           UserTag        = user.Username
           DefaultTracker = org config lookup (jira / github)
           + route.Params (static overrides, e.g. Mode="analyze")
           + IdVar binding (e.g. Query=trailing text)
     5. SaveJobRequest(QueryContext(user.ID, user.OrgID), req)
        → Formicary enforces tenant scope — org_id comes from the
          registered user record, not from the Slack message
     6. Queen replies in thread: "Started ai-jira-query (job abc123)"
```

### CRITICAL: SlackThreadTs — thread reply mechanism

`SlackThreadTs` is the timestamp of the original message.  It is:
- Injected by Formicary as a job variable at submit time
- Stored alongside all other job variables
- Injected into the pod environment by the YAML: `SLACK_THREAD_TS: "{{.SlackThreadTs}}"`
  AND `SlackThreadTs: "{{.SlackThreadTs}}"` (both forms — see gotcha below)
- Read by every script via `config.get("SlackThreadTs") or config.get("SLACK_THREAD_TS")`
- Passed to `chat.postMessage` as `thread_ts` → reply appears in the thread

**GOTCHA**: The YAML must expose it under BOTH names:
```yaml
SLACK_THREAD_TS: "{{.SlackThreadTs}}"
SlackThreadTs: "{{.SlackThreadTs}}"
```
Formicary injects camelCase job variables (e.g. `SlackThreadTs`) but scripts
using `load_config()` read `SLACK_THREAD_TS` (uppercase).  Both must be present
or thread replies silently post to channel root instead of the thread.

### One-time per-developer setup

Each developer DMs the Formicary bot their API token once:

```
DM to @bot:
setup eyJhbGc...  ← Formicary JWT from /dashboard/users/tokens
```

The queen validates the JWT server-side, stores the mapping
`slack_user_id → formicary_user_id` encrypted (AES-256-GCM) in `user_configs`,
and deletes the DM. All subsequent `@bot` mentions from that user are scoped
to their Formicary org automatically.

### Adding a new Slack command

Edit the SlackRoutes admin config via `setup-slack-admin.sh --set-routes`
(no image rebuild, no pod restart):

```json
[
  {"triggers":["my-command","my trigger"],"job_type":"ai-adhoc",
   "description":"What this does","extra_params":{"Skill":"ygs-my-skill"}},

  {"triggers":["my-variant"],"job_type":"ai-jira-query",
   "extra_params":{"Mode":"mymode"},"description":"Variant with Mode=mymode"},

  {"triggers":["my-pipeline"],"job_type":"ai-my-job",
   "id_var":"IssueNumber","description":"New pipeline"}
]
```

Routes take effect immediately — the queen reloads them from the DB at startup
and via `reloadAdminRoutes()`. No router pod exists to restart.

---

## 5. ai-adhoc — The General-Purpose Skill Runner

`job_type: ai-adhoc` is the workhorse for standup, risk, and PR queue.

### What it does (scripts/adhoc/run_skill.py)

1. **Clone you-got-skills** (in YAML script, not Python):
   ```bash
   git clone --depth 1 https://github.com/bhatti/you-got-skills.git /workspace/you-got-skills
   bash /workspace/you-got-skills/setup install   ← installs to ~/.claude/skills/
   ```

2. **Write `.ygs/tracker.yml`** — built dynamically from env vars by `_setup_environment()`:
   ```yaml
   tracker: jira
   project: PROJ
   board_id: 42        # if JIRA_BOARD_ID set
   base_url: https://yourorg.atlassian.net
   bitbucket:
     workspace: myworkspace
     repo: myrepo
   team:
     - alice
     - bob
   ```

3. **Resolve sprint team** — calls Jira `/search?jql=assignee=currentUser() AND sprint in openSprints()`
   to find which sprint boards the current user is on, then collects all unique assignees.
   This populates the `team:` block in `tracker.yml`.

4. **Build prompt** — `_build_prompt()` assembles:
   - SKILL.md content (from `~/.claude/skills/you-got-skills/skills/<skill>/SKILL.md`)
   - Execution environment context (for tracker skills: "no git repo, use Jira API")
   - Sprint team context (authoritative, "do NOT include other assignees")
   - Pre-fetched data section (for `ygs-pr-queue`: embedded `pr_queue.json`)
   - The user's prompt

5. **Run Claude** via `run_claude()` (claude CLI subprocess).

6. **Post to Slack** via `slack_notify(config, text, blocks=blocks)`:
   - `ygs-pr-queue` → `build_pr_blocks()` (Block Kit from `pr_queue.json`)
   - `ygs-standup`, `ygs-risk-scan` → `build_mrkdwn_blocks()` (wraps Claude output)
   - Other skills → plain text

### CRITICAL: Skill search paths (in priority order)

```python
_SKILL_SEARCH_PATHS = [
    Path("/workspace/skills"),
    Path("/workspace/you-got-skills/skills"),
    Path.home() / ".claude" / "skills" / "you-got-skills" / "skills",
    Path.home() / "workplace" / "you-got-skills" / "skills",
]
```

**GOTCHA**: `setup` (the installer script in you-got-skills repo) is named `setup`,
**not** `setup.sh`.  Calling `bash setup.sh install` silently does nothing.  Must be:
```bash
bash /workspace/you-got-skills/setup install
```
Without this, `~/.claude/skills/` is empty, Claude can't find SKILL.md files, and
skills fall back to "apply your best judgment" which produces poor output.

### CRITICAL: Tracker skills need the no-git-repo context

`ygs-risk-scan`, `ygs-standup`, `ygs-pr-queue` run in a bare workspace with no git
checkout.  Without an explicit instruction, Claude will run `ls`, find only a `logs/`
directory, emit "no git repositories found", and fail.

The prompt builder adds this for `_TRACKER_SKILLS`:
```
IMPORTANT: You are running in a bare workspace directory. There is NO git repository.
Do NOT run git commands. All data MUST come from Jira/Bitbucket/GitHub APIs.
The .ygs/tracker.yml file in the current directory has the project/board config.
```

---

## 6. jira-query / jira-analyze

### How the JQL is built (scripts/jira/query_issues.py)

```python
def _build_jql(config, query, issue_type=None):
    parts = [f'project = "{project}"']
    # Team field filter (optional — silently skipped if field not found)
    field_id = _resolve_team_field_id(config, team_field_name)
    if field_id:
        parts.append(f'{field_id} = "{space}"')
    parts.append('status not in ("Done", "Close", "Closed")')
    parts.append(f'summary ~ "{safe_query}"')
    parts.append("ORDER BY priority DESC, created DESC")
```

### CRITICAL: `_resolve_team_field_id()` — dynamic, no hardcoding

Field IDs like `cf[10248]` differ between Jira instances.  The function calls
`GET /rest/api/3/field` and matches by field name (`JIRA_TEAM_FIELD` env var,
default `"EngScrumTeam"`).  Returns `None` gracefully when not found — the filter
is simply skipped.

```python
# Set in YAML env:
JIRA_SPACE: "{{.JiraSpace}}"           # team value, defaults to BitbucketWorkspace
JIRA_TEAM_FIELD: "{{.JiraTeamField}}"  # field name, default "EngScrumTeam"
                                        # set to "" to disable filter entirely
```

### Mode dispatch (in ai-jira-query.yaml script)

```bash
TRACKER="${DEFAULT_TRACKER:-jira}"
if [ "${MODE}" = "analyze" ] && [ "$TRACKER" = "github" ]; then
    python -m scripts.gh.analyze_issues ...
elif [ "${MODE}" = "analyze" ]; then
    python -m scripts.jira.analyze_issues ...
elif [ "$TRACKER" = "github" ]; then
    python -m scripts.gh.query_issues ...
else
    python -m scripts.jira.query_issues ...
fi
```

`jira-analyze` in `workflows.yml` sets `extra_params: {Mode: "analyze"}` so the
router injects `Mode=analyze` into the job without needing a separate job type.
`gh-query` and `gh-analyze` entries set `target_kind: github` so the router injects
`DefaultTracker=github` automatically.

### Smart Tracker Detection

The router infers the right tracker from the message text before checking `DEFAULT_TRACKER`:

| Signal | Detected tracker |
|--------|----------------|
| `bitbucket.org` in URL | `jira` |
| `atlassian.net` in URL | `jira` |
| word `jira` or `bitbucket` in text | `jira` |
| `github.com` in URL | `github` |
| word `github` in text | `github` |
| `jira-query` / `jira query` verb | `jira` (always) |
| `gh-query` / `gh query` verb | `github` (always) |
| no signal | use `DEFAULT_TRACKER` |

### CRITICAL: ADF description extraction

Jira Cloud returns issue descriptions as Atlassian Document Format (ADF) — a
nested JSON tree, not plain text.  `_extract_plain_text()` / `_extract_text_from_doc()`
recursively walks the tree collecting `type: "text"` nodes.  Without this, descriptions
show up as `[object Object]` or are silently empty.

---

## 7. Standup Pipeline (ai-standup-jira)

Three tasks: `gather` → `synthesize` → `post`.

### gather (scripts/standup/gather_jira.py)

**Board selection (two paths):**

1. **Fast path** — `JIRA_BOARDS` set: skips board scan, uses specified board ID(s) directly.
   Set via deploy script flag `--jira-boards <id>` or org config `JiraBoards`.
   Find your board ID in the Jira URL when viewing a sprint board: `.../boards/<ID>`.

2. **Auto-discovery** — `JIRA_BOARDS` empty: scans all scrum boards.
   For each board, fetches active sprint, then paginates through ALL sprint issues (200/page)
   checking if current user's `accountId` or `displayName` appears in any assignee.
   Boards with at least one user-assigned issue are included.

   **CRITICAL**: Uses `GET /rest/agile/1.0/sprint/{sid}/issue` (agile endpoint), NOT JQL.
   JQL sprint-scoped queries return `"total": null` on Jira Cloud — `_board_has_team_issues()`
   always returned `False` with JQL.  The agile endpoint returns actual issue lists.

**Team derivation (board-first):**

- `team_members` is built from actual sprint issue assignees **after** board fetch:
  ```python
  team_members = sorted({i["assignee"] for i in issues if i["assignee"] != "unassigned"})
  ```
- `STANDUP_TEAM_MEMBERS` env var is an optional pre-filter applied during board fetch;
  the assignee-derived list is authoritative for PR filtering and Claude's `TEAM ROSTER`.
- `current_user` is NOT emitted in `signals.json` — it was causing Claude to only analyze
  the API user, missing other team members.

**board_sprint_map**: signals.json now contains:
```json
{
  "board_sprint_map": {
    "<board_id>": {"board": "<board name>", "name": "<sprint name>", "end_date": "<YYYY-MM-DD>"}
  },
  "team_members": ["Alice Smith", "Bob Jones"]
}
```

**PR filtering:** only PRs whose `author` or any `reviewer` is in `team_members` are included.
Previous bug: with empty `STANDUP_TEAM_MEMBERS`, all 250+ org PRs were included.

**Issue tagging:** each normalised issue carries `board_id` so synthesize can group by board.

### synthesize (scripts/standup/synthesize.py)

Reads `signals.json` and calls Claude directly (not via `run_skill.py`):

- Builds prompt from `_SYNTHESIZE_PROMPT` with:
  - `TEAM ROSTER: <json list>` — strict filter: Claude must only report team members
  - `BOARDS: <json map>` — board_id → {board_name, sprint_name, end_date}
  - Full signals JSON (comments trimmed to last 3, bodies truncated at 300 chars)
  - `current_user` popped before sending — Claude must not see it

**Output format (Slack mrkdwn, not markdown):**
- `*Bold*` section headers (single star — valid Slack bold). NOT `**double-star**` or ALL CAPS.
- `•` bullets (never `-`)
- `<url|KEY>` hyperlinks (Slack format)
- No markdown tables
- Risk emoji: 🔴 HIGH, 🟡 MEDIUM, ℹ️ LOW

**Board grouping:** when issues span multiple boards, output has one section per board.
When all issues share one board, output is a single combined section.

**Output sections (parsed from Claude output):**
```
#### STANDUP_BRIEF   →  standup_brief.md
#### RISK_REPORT     →  risk_report.md
```

`_strip_markdown()` converts `**double-star**` to `*single-star*` and dash bullets to `•`,
but never removes existing `*single-star*` (used as Slack section headers).

**CRITICAL**: All issue and PR references in output MUST use `<url|PROJ-NNN>` Slack
hyperlink format.  Plain text links or bare keys without hyperlinks are wrong.

### post (scripts/standup/post.py)

- Reads `standup_brief.md`
- Calls `notify(config, text, blocks=build_mrkdwn_blocks(text))`
- Posts to `SlackChannel` org config (NOT the per-job `SlackChannel` variable for ad-hoc)

### CRITICAL: Standup cron vs ad-hoc channel routing

The standup cron job reads `SlackChannel` from org configs.
Ad-hoc jobs (`ai-adhoc`) read `SlackChannel` from the job variable injected by the router.
They are different sources.  The standup's `SLACK_CHANNEL` env in the YAML is:
```yaml
SLACK_CHANNEL: "{{.SlackChannel}}"   # from org config via job variable
```

---

## 8. Implement Pipeline (ai-jira-implement)

Sequential Formicary tasks, each visible in task log:
```
plan (15m)  →  implement (90m)  →  self-review (15m)  →  create-pr (10m)  →  poll-pr (repeating)
```

**self-review task:** runs `scripts/review/run.py --mode self-review`. Claude diffs `BASE...HEAD`, writes `self_review.json` (`APPROVED`/`NEEDS_FIX`/`BLOCKED`). Exit code 2 = BLOCKED → Formicary pauses job for human review before the PR is created.

**Complexity-tiered models:** the `plan` task writes `plan_complexity.txt` (`low`/`medium`/`high`). The `implement` task reads it and selects the model tier: `AnthropicComplexityLowModel` (Haiku), default Sonnet, `AnthropicComplexityHighModel` (Opus). Override via org configs pushed by `deploy-ai-workflows.sh --set-configs`.

### Task sub-steps (each shows as a named step in Formicary's task log)

**plan task:**
```
scripts.jira.issue_picker   — fetch Jira issue, write issue.json
scripts.jira.plan           — Claude /ygs-wbs, write plan.md
```

**implement task:**
```
scripts.jira.clone_repo     — clone repo, create branch, write branch.txt
scripts.jira.run_implement  — Claude implements, writes impl_run_result.json
scripts.jira.push_impl      — commit_all + push, writes impl_result.json
```

Exit codes from `push_impl`: `0`=done, `1`=tests failing, `2`=blocked/max-turns.
Formicary maps `2 → PAUSE_JOB` (human review needed) via `on_exit_code`.

**create-pr task:**
```
scripts.jira.build_pr       — BitBucket REST API create PR, write pr.json
scripts.jira.notify_pr      — Jira label transition + Slack notification
```

**poll-pr task (repeats until PR merged/declined):**
```
scripts.jira.check_pr_state — fetch PR state; on MERGED/DECLINED: run learn, notify Slack, exit 0
scripts.jira.fetch_comments — fetch unprocessed comments; filter ai-bot prefix; write pending_comments.json
scripts.jira.respond_comments — clone repo, Claude per comment, push, post reply; exit 3 if PR still open
```

Exit code `3 → PAUSE_JOB` → Formicary waits `PollInterval` seconds then retries.

### CRITICAL: MAX_TURNS_IMPLEMENT must be 200

Claude spent all 100 turns exploring the repo without writing code on large repos.
The YAML sets:
```yaml
MaxTurnsImplement: "200"
```
And the implement prompt has a prominent directive:
```
IMPORTANT: You have a limited number of turns. Start implementing IMMEDIATELY.
Trust the plan — it already identifies the files. Go straight to editing.
Run only tests directly covering the changed code (not the full suite per task).
After ALL tasks are done, run the full test suite once.
```

### CRITICAL: implement runs in the repo directory

`clone_repo` clones the repo to `/workspace/repo/`. `run_implement` runs `claude`
with `working_dir` set to that directory.  Claude has Bash, Read, Write, Edit tools.
CLAUDE.md in the target repo is the primary instruction set — it overrides everything.

### Poll PR / pause-resume

`check_pr_state` runs first every poll cycle. On MERGED/DECLINED it writes
`poll_state.json {terminal: true}` — the downstream steps (fetch_comments,
respond_comments) both check this file and skip immediately (exit 0), so the
whole task exits 0 and the job completes normally.

For the review workflow (`ai-jira-review`, `ai-gh-review`) the `await-feedback`
task exits with code `3` → Formicary maps `3 → PAUSE_JOB`.
The router resumes it via 4-step pattern:
```
GET /api/jobs/requests/{id}  →  merge Decision var  →  PUT  →  POST /trigger
```

---

## 9. Block Kit Output

All skill output posts to Slack as structured Block Kit, not plain text walls.

### Helpers in scripts/standup/slack_client.py

| Function | Input | Use case |
|----------|-------|---------|
| `build_issue_blocks(title, issues, base_url)` | Jira issue list dicts | `jira-query` results |
| `build_pr_blocks(title, pr_data)` | `pr_queue.json` dict | `ygs-pr-queue` output |
| `build_mrkdwn_blocks(text)` | Plain mrkdwn string | `ygs-standup`, `ygs-risk-scan` output |

### CRITICAL: Always pass `text` alongside `blocks`

Slack requires `text` even when `blocks` is present.  It is used as:
- Push notification preview
- Accessibility fallback
- Shown when blocks can't render

```python
notify(config, fallback_text, blocks=blocks)   # correct
notify(config, blocks=blocks)                  # wrong — API error
```

### Issue row format (from shared/output-format.md)

```
• <url|PROJ-NNN> [Type] Summary — _Assignee_ · Status · Priority · Nd old
```

The skills are instructed that omitting the `<url|KEY>` link format is a **wrong answer**.

### CRITICAL: Team filter enforcement

Both `ygs-standup` and `ygs-risk-scan` must filter to the `team:` list in `.ygs/tracker.yml`.
Items from other sprint boards or unrelated assignees must not appear.
This is enforced in `SKILL.md` and `output-format.md` with explicit instructions.
Without it, risk scan shows users from other boards (the original bug).

---

## 10. you-got-skills Integration

### How skills are installed in each job pod

Skills are installed by `entrypoint.sh` when the container starts (once per pod lifetime,
guarded by a marker file `~/.claude/.ygs-installed`):

```bash
YGS_INSTALL_DIR="${HOME}/.claude/skills/you-got-skills"
git clone --depth 1 https://github.com/bhatti/you-got-skills.git "${YGS_INSTALL_DIR}"
# Symlink each skill directory individually (NOT the setup installer script)
for skill_dir in "${YGS_INSTALL_DIR}"/skills/ygs-*; do
    ln -snf "$skill_dir" "${HOME}/.claude/skills/$(basename "$skill_dir")"
done
```

**CRITICAL — why the old approach was broken**: The previous approach cloned to `/tmp/ygs`,
ran `bash /tmp/ygs/setup install` (which symlinked `/tmp/ygs` → `~/.claude/skills/you-got-skills`),
then deleted `/tmp/ygs`.  Every skill symlink immediately became a dangling reference.
The fix: clone **directly** to the final destination `~/.claude/skills/you-got-skills` and
create per-skill symlinks explicitly.

**GOTCHA**: `setup` installs relative to `$HOME`.  In K8s pods, `$HOME` is usually
`/root` (or `/home/agent` for non-root containers).  The install path must match
`_SKILL_SEARCH_PATHS` in `run_skill.py`.

### Shared skill files referenced by tracker skills

```
skills/shared/
  init.md            ← Step 1 for standup/risk-scan/sprint-plan
  tracker.md         ← JQL query patterns for Jira/GitHub
  slack.md           ← Slack token check + get_standup_messages
  risk-criteria.md   ← Severity classification table
  output-format.md   ← CANONICAL row format + team filter enforcement
  ownership-principles.md
```

`output-format.md` is the single source of truth for output format.
All tracker skills reference it.  Changing row format: edit this file.

### Key SKILL.md files

| Skill | Key behaviour |
|-------|-------------|
| `ygs-standup` | Gathers signals, synthesizes per-person, calls ygs-risk-scan for risks section |
| `ygs-risk-scan` | Builds dependency graph, capacity check, ranks HIGH/MEDIUM/LOW |
| `ygs-pr-queue` | Formats pre-fetched `pr_queue.json` — does NOT make API calls |
| `ygs-implement` | Plans + implements a ticket — uses CLAUDE.md as primary instruction |
| `ygs-review-pr` | Four parallel review passes: correctness, security, API, SRE |
| `ygs-review-deep` | Seven-domain deep review: standard four + performance, testing quality, architecture |

---

## 10b. Extra Skills Repos — EXTRA_SKILLS_REPOS

Any job pod can install additional skill repositories at startup via `EXTRA_SKILLS_REPOS`.
This is handled by `_ensure_extra_skills()` in `scripts/common/claude_runner.py`.

### Format

`EXTRA_SKILLS_REPOS` is read as JSON array **or** a plain string:

| Value | Expansion |
|-------|-----------|
| `myrepo` | `https://bitbucket.org/$BITBUCKET_WORKSPACE/myrepo.git` (DEFAULT_TRACKER=jira) |
| `myrepo` | `https://github.com/$GH_ORG/myrepo.git` (DEFAULT_TRACKER=github) |
| `org/repo` | `https://bitbucket.org/org/repo.git` or `https://github.com/org/repo.git` |
| Full URL | Used as-is |
| JSON array | Full control (see below) |

```bash
# Bare name (auto-expands using DEFAULT_TRACKER + BITBUCKET_WORKSPACE or GH_ORG)
EXTRA_SKILLS_REPOS=my-skills-repo

# JSON array — full control
EXTRA_SKILLS_REPOS='[
  {
    "url": "https://bitbucket.org/myorg/myrepo.git",
    "skills_dir": "skills",        # default: auto-detect (.claude/skills → skills → .skills)
    "sparse": true,                # default: true  — sparse-checkout only skills_dir
    "token_env": "BITBUCKET_TOKEN", # default: auto-detected from URL hostname
    "username_env": "BITBUCKET_USERNAME"
  },
  {
    "url": "https://github.com/org/agent-skills",
    "type": "skills-cli"           # delegate to npx skills add (vercel-labs/skills CLI)
  }
]'
```

### Sparse checkout (default: true)

Uses `git clone --depth 1 --filter=blob:none --sparse` then
`git sparse-checkout set <skills_dir>` — downloads only the skills subtree.
Essential for large monorepos.  Set `"sparse": false` only for dedicated skills repos.

### Auto-credential detection

Credentials are injected automatically from existing env vars:

| URL pattern | Token env | Username env |
|------------|-----------|-------------|
| `bitbucket.org` | `BITBUCKET_TOKEN` | `BITBUCKET_USERNAME` |
| `github.com` | `GH_TOKEN` | — |

Override with explicit `token_env` / `username_env` in the JSON entry.

### type: "skills-cli" — vercel-labs/skills

For repos that follow the [vercel-labs/skills](https://github.com/vercel-labs/skills) format,
set `"type": "skills-cli"` to delegate to `npx skills add <repo> --agent claude-code --yes`.
The CLI installs to `~/.claude/skills/` automatically.

**Compatibility**: you-got-skills SKILL.md files (frontmatter `name:` + `description:`) are
compatible with the vercel-labs/skills spec.

**Constraints**:
- Does NOT support sparse checkout — avoid for large monorepos
- Requires Node.js / npx in the container (already available in `plexobject/ai-dev-tools`)
- Uses GH_TOKEN automatically for private GitHub repos

### Skills dir auto-detection

When `skills_dir` is not specified (non-skills-cli path), the following directories are
tried in order: `.claude/skills` → `skills` → `.skills`.  First match wins.

### YAML template param vs org config

**IMPORTANT**: When passing `ExtraSkillsRepos` as a Formicary job param (i.e. via
`{{.ExtraSkillsRepos}}` in a YAML task environment block), the value undergoes YAML template
substitution before parsing. JSON values containing `"`, `{`, or `}` will break YAML parsing.

**Rules:**
- **Org config (recommended for JSON)**: Set `EXTRA_SKILLS_REPOS` env var before running the
  deploy script. The value is stored via the Formicary org config API and injected at runtime
  via the K8s secret path — YAML substitution does not apply. Full JSON works here.
- **Job param (plain strings only)**: Use simple URL/name strings (`ExtraSkillsRepos` param).
  These go through YAML template substitution, so avoid JSON syntax.

```bash
# Org config — full JSON + skills-cli works (run before deploy-ai-*.sh):
export EXTRA_SKILLS_REPOS='[
  {"url":"https://github.com/bhatti/you-got-skills.git","sparse":false},
  {"url":"nutlope/hallmark","type":"skills-cli"}
]'
bash deploy-ai-jira-workflows.sh --set-configs

# Job param — plain URL/name only (in Formicary job submission API or Slack command params):
ExtraSkillsRepos = https://github.com/bhatti/you-got-skills.git
```

### MAX_CLAUDE_PROCESS_TIMEOUT

Set `MAX_CLAUDE_PROCESS_TIMEOUT` (seconds) to kill the Claude process if it runs too long.
Prevents the Formicary task-level timeout (25m) from being the first line of defence.

```bash
MAX_CLAUDE_PROCESS_TIMEOUT=270   # kill after 4.5 min; leaves ~30s for cleanup
```

The timeout is enforced via `threading.Timer` + `os.killpg()` in `run_claude()`.

**CRITICAL implementation detail**: `proc.kill()` alone does NOT work because Claude spawns
child processes that keep the stdout pipe open, causing the drain loop to block forever.
The fix uses `os.killpg(os.getpgid(proc.pid), SIGKILL)` to kill the entire process group.
This requires `start_new_session=True` in `subprocess.Popen` so the Claude process gets its
own session/process group. See `run_claude()` in `scripts/common/claude_runner.py`.

The timeout can also be set per-call via the `process_timeout` parameter.

---

## 11. Environment Variable Propagation — Complete Map

```
┌─────────────────────────────────────────────────────────────┐
│  K8s Secret (ai-dev-credentials)                             │
│  JIRA_API_TOKEN, SLACK_BOT_TOKEN, SSH_PRIVATE_KEY, etc.     │
└─────────────────┬───────────────────────────────────────────┘
                  │ env_from: secret_ref
┌─────────────────▼───────────────────────────────────────────┐
│  Formicary YAML environment: block                           │
│  JIRA_BASE_URL: "{{.JiraUrl}}"    ← from org config         │
│  SLACK_CHANNEL: "{{.SlackChannel}}"                         │
│  SLACK_THREAD_TS: "{{.SlackThreadTs}}"  ← from router       │
│  SlackThreadTs:  "{{.SlackThreadTs}}"  ← BOTH needed        │
│  MODE: "{{.Mode}}"                ← from extra_params       │
└─────────────────┬───────────────────────────────────────────┘
                  │ os.environ in pod
┌─────────────────▼───────────────────────────────────────────┐
│  scripts/common/config.py  load_config()                     │
│  - reads os.environ                                          │
│  - applies short aliases (BB_REPO → BITBUCKET_REPO etc.)    │
│  - applies DEFAULTS for unset vars                           │
│  → config dict passed to every script                        │
└─────────────────────────────────────────────────────────────┘
```

**GOTCHA**: Formicary injects org configs as camelCase template variables
(e.g. `JiraUrl`, `SlackChannel`).  The YAML `environment:` block must explicitly
map them to UPPER_SNAKE_CASE env vars that the scripts expect.
A missing mapping means a script gets an empty string and silently skips work
(e.g. Slack post not sent, Jira query uses wrong project).

---

## 12. Deploy Workflow — The Correct Order

Run these steps **in order** after any code or YAML change.  Skip steps that do not apply.

### Step 1 — Build and push the Docker image (code changes only)

```bash
cd ~/workplace/ai-dev-tools

# Always use NO_CACHE=1 to guarantee fresh layers (buildx reuses amd64 cache otherwise)
NO_CACHE=1 make build

# Push after a successful build
make push
```

If the build fails with a transient network error (e.g. curl downloading jira-cli),
retry once — it's almost always a temporary GitHub release download hiccup.

### Step 2 — Clear the k8s image cache and pull the new image

`image_pull_policy: Always` is set, but containerd caches image digests.  If the
node already has 80+ `plexobject/ai-dev-tools` references, it reuses the cached
digest even after a new push.  Force a fresh pull by clearing the cache:

```bash
# Run a privileged pod on the k3s node to clear containerd's image cache
kubectl run clear-img-cache --image=ubuntu --restart=Never --privileged \
  --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"k3s-node"}}}' \
  -- /bin/sh -c "crictl rmi --prune 2>/dev/null; \
     ctr images rm \$(ctr images ls -q | grep ai-dev-tools) 2>/dev/null; true"

# Wait for the pod to finish, then clean it up
kubectl wait --for=condition=completed pod/clear-img-cache --timeout=30s 2>/dev/null || true
kubectl delete pod clear-img-cache --ignore-not-found=true
```

After this, the next job run will pull the new image from the registry.

You can verify the pull succeeded by checking the first job's pod events:
```bash
kubectl describe pod <job-pod-name> | grep -A5 "Events:"
# Look for: "Successfully pulled image" with the new digest
```

### Step 3 — Redeploy workflow YAML definitions to Formicary (YAML changes only)

YAML changes do **not** require an image rebuild — just redeploy:

```bash
cd ~/workplace/formicary/docs/examples

# Jira-based workflows (standup + review + implement + adhoc)
bash deploy-ai-standup-jira.sh          # standup cron + on-demand
# Or deploy all at once:
bash deploy-ai-jira-workflows.sh

# GitHub-based workflows (if changed)
bash deploy-ai-workflows.sh

# (Optional) Update Slack route table — only when routes change
FORMICARY_TOKEN=... SLACK_BOT_TOKEN=... SLACK_APP_TOKEN=... \
  bash setup-slack-admin.sh --set-routes
```

The Formicary queen reloads job definitions from the DB immediately after the PUT.
No queen pod restart is needed.

### Step 4 — (Optional) Port-forward if running locally in kind/K8s

```bash
kubectl port-forward svc/formicary 7777:7777 19000:19000
```

Not needed when connecting to the EC2-hosted cluster directly via `EC2_IP`.

**GOTCHA**: The formicary queen image is separate from the ai-dev-tools job image.
If the queen pod CrashLoops after a restart (SeaweedFS LOCK file), `removeStaleLocks()`
in `internal/artifacts/server_local.go` handles it automatically.

---

## 13. Testing

### 13a. Unit tests (fast — run before every commit)

```bash
cd ~/workplace/ai-dev-tools

# Full unit test suite — must pass before any commit
python3 -m pytest tests/ -q

# Current count: ~276 tests
```

Key unit test files:
```
tests/test_standup_slack_client.py   ← slack_client helpers, Block Kit builders, thread_ts
tests/test_jira_query_issues.py      ← JQL builder, _format_issue, main with mock
tests/test_jira_analyze_issues.py    ← key extraction, ADF text, analyze main
tests/test_standup_router.py         ← router intent resolution, registry
tests/test_standup_registry.py       ← WorkflowEntry, extra_params, resolve()
```

**GOTCHA**: `_resolve_team_field_id` lives in `scripts.jira.query_issues`, NOT in
`analyze_issues`.  When mocking it in analyze tests:
```python
@patch("scripts.jira.query_issues._resolve_team_field_id", return_value=None)
```
NOT `scripts.jira.analyze_issues._resolve_team_field_id`.

### 13b. Functional / end-to-end tests (slow — run after any rebuild/redeploy)

Functional tests submit real jobs to the live Formicary cluster, wait for completion,
download the task artifact ZIP, and verify expected files exist inside it.
They also check task context variables (SKILL, SKILL_LOADED, SKILLS_INVOKED, etc.).

#### Prerequisites

```bash
# Required environment variables (add to ~/.zshrc):
export EC2_IP=10.8.97.24.nip.io           # or your Formicary host
export FORMICARY_TOKEN=<your-api-token>
export PR_URL=https://bitbucket.org/cribl/cribl/pull-requests/45974  # for review tests
export JIRA_BOARDS=<board-id>             # optional — speeds up standup/prs tests

# Derived automatically:
# FORMICARY_URL=https://${EC2_IP}
```

#### Full rebuild → redeploy → functional test sequence

```bash
# 1. Build + push new image
cd ~/workplace/ai-dev-tools
NO_CACHE=1 make build && make push

# 2. Clear k8s image cache so the node pulls the new image
kubectl run clear-img-cache --image=ubuntu --restart=Never --privileged \
  --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"k3s-node"}}}' \
  -- /bin/sh -c "crictl rmi --prune 2>/dev/null; \
     ctr images rm \$(ctr images ls -q | grep ai-dev-tools) 2>/dev/null; true"
kubectl wait --for=condition=completed pod/clear-img-cache --timeout=30s 2>/dev/null || true
kubectl delete pod clear-img-cache --ignore-not-found=true

# 3. Redeploy workflow YAMLs
cd ~/workplace/formicary/docs/examples
bash deploy-ai-standup-jira.sh
bash deploy-ai-jira-workflows.sh

# 4. Run functional tests
cd ~/workplace/ai-dev-tools
FORMICARY_URL=https://10.8.97.24.nip.io \
  python3 tests/test_functional_workflows.py \
    --tests standup,standup-post,review,review-post \
    --timeout 1200
```

#### Available test names

| Name | Job type | Task | Validates |
|------|----------|------|-----------|
| `standup` | `ai-standup-jira` | synthesize | brief written, reports/, context keys |
| `standup-post` | `ai-standup-jira` | post | reports/report.md/html, slack_message.txt |
| `review` | `ai-jira-review` | review | findings.json, context keys (SKILL, SKILLS_INVOKED) |
| `review-post` | `ai-jira-review` | post | reports/findings.json, slack_message.txt |
| `prs` | `ai-adhoc` | run | open PR queue, context keys |
| `risks` | `ai-adhoc` | run | risk scan report |
| `pr-comments` | `ai-adhoc` | run | PR comments report |
| `ask` | `ai-adhoc` | run | general Q&A via ygs-ask |
| `extra-skills` | `ai-adhoc` | run | EXTRA_SKILLS_REPOS install + SKILLS_INVOKED |
| `jira-query` | `ai-jira-query` | query | reports/report.md, result.json |
| `gh-analyze` | `ai-jira-query` | query | reports/report.md, result.json |

Run a subset:
```bash
# Core pipeline (standup + review end-to-end):
python3 tests/test_functional_workflows.py --tests standup,standup-post,review,review-post

# Optional extras:
python3 tests/test_functional_workflows.py --tests prs,risks

# All tests (requires PR_URL in env):
python3 tests/test_functional_workflows.py --tests all --timeout 1200
```

#### What each test verifies

**standup** (synthesize task):
- `standup_brief.md` exists in artifact ZIP
- `reports/report.md` and `reports/report.html` exist
- Task context has `SELECTED_MODEL`, `SELECTED_TRACKER`, `ISSUE_COUNT`

**standup-post** (post task):
- `reports/report.md` — final combined brief + risk report
- `reports/report.html` — rich board-status HTML from render_html.py
- `reports/post_result.json` — Slack post status
- `reports/slack_message.txt` — exact text sent to Slack (verifiable without Slack access)

**review** (review task):
- `findings.json` + `reports/findings.json` exist
- `reports/report.md` and `reports/report.html` exist
- Task context has `SKILL`, `SKILL_LOADED`, `YGS_SKILLS_COUNT`, `YGS_SKILLS_INSTALLED`,
  `YGS_SKILLS_REPO_COMMIT`, `SKILLS_INVOKED`

**review-post** (post task):
- `reports/findings.json` — findings used by the post task
- `reports/report.md` and `reports/report.html` — rendered review report
- `reports/post_result.json` — Slack post status
- `reports/slack_message.txt` — exact text sent to Slack

#### Artifact convention — all outputs go to `reports/`

Every task writes its outputs to `reports/` so YAML artifact paths never need
changing:

| File | Written by | Contains |
|------|-----------|----------|
| `reports/report.md` | synthesize / post / run | Final markdown report |
| `reports/report.html` | render_html / post_findings | Rich HTML report |
| `reports/result.json` | synthesize / adhoc | Status JSON from Claude |
| `reports/findings.json` | run.py (review) | Raw findings from Claude |
| `reports/post_result.json` | post / post_findings | Slack post outcome |
| `reports/slack_message.txt` | post / post_findings | Full Slack message text |

All YAML `artifacts.paths` should be `- ./reports` — never list individual files.

---

## 14. All Known Gotchas (Consolidated)

| # | Gotcha | Symptom | Fix |
|---|--------|---------|-----|
| 1 | `SLACK_THREAD_TS` vs `SlackThreadTs` | Replies post to channel root | YAML must expose BOTH names as separate env vars |
| 2 | `setup` not `setup.sh` | Skills not found, Claude uses fallback prompts | `bash /workspace/you-got-skills/setup install` |
| 3 | No git repo context | "no git repositories found" from risk-scan | `_TRACKER_SKILLS` prompt injection: "no git repo, use Jira API" |
| 4 | Hardcoded field IDs | `cf[10248]` doesn't exist on other Jira instances | `_resolve_team_field_id()` resolves dynamically via `/rest/api/3/field` |
| 5 | ADF descriptions | Descriptions show as `[object Object]` or empty | `_extract_plain_text()` recursively walks ADF JSON |
| 6 | MAX_TURNS too low | Claude exhausts 100 turns exploring, 0 commits | Set `MaxTurnsImplement: "200"` in YAML; prompt: "start implementing immediately" |
| 7 | Org config camelCase → env UPPER_SNAKE | Script gets empty string, silently skips | Every camelCase var must be explicitly mapped in YAML `environment:` block |
| 8 | Team filter missing | Risk scan shows unrelated users/boards | `team:` in `.ygs/tracker.yml` + SKILL.md "NEVER include others" instruction |
| 9 | `text` missing alongside Block Kit `blocks` | Slack API 400 error | Always pass both: `post_message(config, text, blocks=blocks)` |
| 10 | implement runs with `--dangerously-skip-permissions` | Expected — Claude needs filesystem access in the cloned repo | This is intentional, not a security bug |
| 11 | `image_pull_policy: Always` — no need to reload into kind | Confusion about why old code runs after a push | Image is always pulled fresh per job; only router pod needs restart |
| 12 | Formicary pause/resume needs 4-step GET→merge→PUT→trigger | `POST /trigger` alone doesn't inject new variables | See `formicary_client.py: resume()` for the pattern |
| 13 | `JIRA_TEAM_FIELD=""` disables team filter | Useful when field doesn't exist on the instance | Default `"EngScrumTeam"` — set to `""` to skip |
| 14 | SeaweedFS LOCK file on queen restart | Queen pod CrashLoops | `removeStaleLocks()` in `server_local.go` handles automatically |
| 15 | `jira-analyze` uses `Mode: "analyze"` via `extra_params` | It's the same `ai-jira-query` job type — no separate YAML | `extra_params` in `workflows.yml` injects at submit time |
| 16 | JQL sprint-scoped queries return `"total": null` on Jira Cloud | `_board_has_team_issues()` always returns `False`, board scan finds 0 boards | Use `GET /rest/agile/1.0/sprint/{sid}/issue` (agile endpoint), NOT JQL |
| 17 | Standup shows only current user, missing team members | `current_user` in signals caused Claude to anchor analysis on one person | `current_user` removed from signals; team derived from sprint assignees; `TEAM ROSTER` injected in prompt |
| 18 | 250+ org PRs included in standup | Empty `STANDUP_TEAM_MEMBERS` → no PR filter applied | PRs filtered post-fetch against `team_members` derived from board assignees |
| 19 | you-got-skills symlinks broken in container | Clone to `/tmp` + `setup install` + `rm -rf /tmp/ygs` → dangling symlinks | Clone directly to `~/.claude/skills/you-got-skills`; symlink each `skills/ygs-*` explicitly |
| 20 | `_strip_markdown` was removing Slack bold `*headers*` | Single-star bullets/headers stripped → blank standup sections | Only convert `**double-star**` to `*single-star*`; never strip existing single-star |
| 21 | containerd caches image digest even with `imagePullPolicy: Always` | Old code runs after `make push`; node shows 80+ `plexobject/ai-dev-tools` refs | Run `crictl rmi --prune` + `ctr images rm` on the k3s node to force fresh pull |
| 22 | `---` stop pattern in `_truncate_brief` | Standup Slack shows only 2 lines (cut at first `---` section separator) | Removed `^---\s*$` from stop_patterns; Claude uses `---` as valid section dividers |
| 23 | `post_findings.py` uses `/workspace/findings.json` but artifact zip only has `reports/` | Review post shows "Review artifacts not found" | Use `reports/findings.json`; all artifact paths must be under `reports/` |
| 24 | Slack `conversations.list` rate-limit (429) with 2200+ channels | Standup gather takes 5+ minutes hitting 30s Retry-After per page | Cap retry wait at 5s; increase page size to 1000; cache channel IDs per token |

---

## 15. Multi-User Infra — Per-User Ant Workers

When multiple team members share one Formicary leader, each runs a personal ant
worker on their laptop tagged with their user ID (`$USER`).  Jobs route exclusively
to their ant via `pod_labels`.

### Setup (one-time per team member)

```bash
cd ~/workplace/formicary
./scripts/setup-ant-worker.sh --queen <ec2-host> --token $FORMICARY_TOKEN
# Sets up formicary-ant-credentials secret + applies k8s/formicary-ant.yaml
```

See `formicary/docs/ant-worker-setup.md` for the full guide.

### How user routing works

Every AI workflow YAML has:
```yaml
job_variables:
  UserTag: ""            # empty = any ant; "alice" = routes to alice's ant

tasks:
  - task_type: implement
    pod_labels:
      user: "{{.UserTag}}"
```

The Slack router reads `USER_TAG` from org config and injects it at submit time.
Formicary matches `pod_labels` against each ant's registered labels.

### Setting your UserTag

```bash
./docs/examples/deploy-ai-workflows.sh --set-configs --ant-user-tag "${USER}"
# Sets UserTag org config → router injects it → all your jobs go to your ant
```

### K8s manifests

| Manifest | Use |
|----------|-----|
| `formicary/k8s/formicary-all-in-one.yaml` | Local single-node dev (leader + embedded ant) |
| `formicary/k8s/formicary-leader.yaml` | EC2 leader-only (external ants connect via WebSocket) |
| `formicary/k8s/formicary-ant.yaml` | Per-user ant worker on laptop |

---

## 16. SQLite Performance — WAL Mode

The Formicary queen database uses SQLite WAL mode for safe concurrent access.
WAL is configured automatically — no manual pragma needed.

Settings applied to every SQLite DSN:
```
_journal_mode=WAL      — concurrent readers + 1 writer
_busy_timeout=5000     — 5s retry on SQLITE_BUSY before failing
_synchronous=NORMAL    — safe with WAL, much faster than FULL
_cache_size=-64000     — 64MB page cache
_foreign_keys=ON       — referential integrity
```

The WebSocket buffer database (`internal/queue/websocket_buffer_sqlite.go`) also
applies WAL, busy_timeout=3000ms, and synchronous=NORMAL after open.

To override defaults (e.g. lower busy timeout for testing):
```yaml
# formicary-queen.yaml
db:
  sqlite_wal_mode: true
  sqlite_busy_timeout_ms: 3000
  sqlite_synchronous: "NORMAL"
  sqlite_cache_size_kb: 64000
```

---

## 17. Codebase-Local Skill Override

Skills in `.claude/skills/<name>/SKILL.md` inside your project repo take priority
over global you-got-skills.  No router changes needed.

**Search path (in priority order):**
1. `$CODEBASE_DIR/.claude/skills/<name>/SKILL.md` — codebase-local (highest priority)
2. `/workspace/you-got-skills/skills/<name>/SKILL.md` — global ygs (container)
3. `~/.skills/you-got-skills/skills/<name>/SKILL.md` — local dev fallback

`CODEBASE_DIR` is set by the implement/review workflow YAMLs to `/workspace/repo`
(the cloned project repo).  For ad-hoc jobs it defaults to empty (no codebase-local
override unless explicitly set).

For local dev without a container:
```bash
cd ~/workplace/ai-dev-tools
make install-skills   # clones you-got-skills into ~/.skills/
```

---

## 18. File Locations — Quick Reference

```
~/workplace/
├── formicary/
│   └── docs/examples/
│       ├── ai-adhoc.yaml               ← general skill runner
│       ├── ai-standup-jira.yaml        ← daily standup cron
│       ├── ai-jira-implement.yaml      ← implement pipeline
│       ├── ai-jira-review.yaml         ← PR review + pause/resume
│       ├── ai-jira-query.yaml          ← jira-query + jira-analyze
│       ├── deploy-ai-jira-workflows.sh ← deploys all Jira workflows
│       └── deploy-ai-workflows.sh      ← deploys GitHub workflows
│
├── ai-dev-tools/
│   ├── scripts/
│   │   ├── common/
│   │   │   ├── config.py               ← load_config(), env alias resolution
│   │   │   ├── claude_runner.py        ← run_claude() subprocess wrapper
│   │   │   └── jira_api.py             ← Jira REST v3 client
│   │   ├── slack/
│   │   │   ├── router.py               ← Bolt Socket Mode app
│   │   │   ├── registry.py             ← WorkflowEntry, resolve()
│   │   │   ├── workflows.yml           ← Slack command → job_type mapping
│   │   │   └── formicary_client.py     ← submit/find_jobs/resume
│   │   ├── adhoc/
│   │   │   └── run_skill.py            ← skill runner: tracker.yml, team, blocks
│   │   ├── standup/
│   │   │   ├── slack_client.py         ← post_message, notify, build_*_blocks
│   │   │   ├── gather_jira.py          ← sprint issues, PR fetch
│   │   │   └── gather_pr_queue.py      ← pr_queue.json builder
│   │   └── jira/
│   │       ├── query_issues.py         ← jira-query: JQL builder + Block Kit output
│   │       └── analyze_issues.py       ← jira-analyze: ADF extraction + Claude
│   ├── docs/
│   │   ├── system-reference.md         ← THIS FILE
│   │   ├── slack-router.md             ← detailed router flow
│   │   ├── architecture.md             ← component diagram
│   │   ├── configuration.md            ← all env vars
│   │   └── slack-setup.md              ← Slack app creation
│   └── tests/                          ← 276 tests, must all pass
│
└── you-got-skills/
    └── skills/
        ├── shared/
        │   ├── output-format.md        ← CANONICAL row format (no hardcoding)
        │   ├── brief-format.md         ← standup output rules
        │   ├── tracker.md              ← JQL patterns for Jira/GitHub
        │   └── risk-criteria.md        ← HIGH/MEDIUM/LOW classification
        ├── ygs-standup/SKILL.md
        ├── ygs-risk-scan/SKILL.md
        └── ygs-pr-queue/SKILL.md
```

---

## 16. What a New AI Session Should Do First

1. **Read this file** (`docs/system-reference.md`) — you're doing that now.
2. **Read `docs/architecture.md`** for the component diagram.
3. **Read `docs/configuration.md`** for all env vars.
4. **Run `python3 -m pytest tests/ -q`** — all tests must pass before any change.
5. **Never run git commands** — user handles all git operations.
6. **Never hardcode** org names, project keys, usernames, URLs, field IDs — everything derives from env vars.
7. **After any code change**: `make docker-build` → `./setup-ant-worker.sh` (pulls new image into k8s nodes).
8. **After any YAML change**: `bash deploy-ai-jira-workflows.sh` (no image rebuild needed).
9. **There is no router pod** — Slack routing is built into the Formicary queen. No `ai-slack-router` deployment exists.
