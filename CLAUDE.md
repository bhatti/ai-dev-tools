# ai-dev-tools Development Guide

## CRITICAL RULES

### Rule #0: MANDATORY TESTING PYRAMID — unit → pod → e2e
**EVERY code change MUST be validated in this order before claiming it works:**

```
1. Unit tests (fast, no network, no k8s)
   python3 -m pytest tests/test_<module>.py -v

2. Pod functional tests (real k8s pod, local scripts copied in, no full job cycle)
   source ~/.zshrc
   python3 tests/test_pod_functional.py --tests <test-name>

3. E2E job (full Formicary job: deploy workflow YAML + submit job)
   source ~/.zshrc
   bash /path/to/deploy-ai-workflows.sh --set-configs
   curl -sk -X POST "${FORMICARY_URL}/api/jobs/requests" ...
```

**NEVER skip to e2e without running pod tests first.**
Pod tests catch 90% of runtime failures (module not found, wrong path, bad env var, file missing)
in 30-60 seconds vs. the 5-30 minute e2e cycle.

**Why this order matters:**
- Unit tests verify logic in isolation
- Pod tests verify the script runs in the exact container environment (same image, same filesystem layout, same env vars) — catches issues like missing modules, bad imports, wrong paths
- E2E tests verify the full Formicary orchestration (artifact downloads, task chaining, cron triggers)

**For every new script added to a workflow task:**
1. Write `tests/test_<script>.py` — unit tests
2. Add a test case to `tests/test_pod_functional.py` — pod test
3. Only run e2e after both pass

---

### Rule #1: NEVER hardcode IPs, hostnames, or project names
**ALL environment-specific values MUST come from environment variables.**


# ✅ CORRECT — always from environment
EC2_IP = os.environ.get("EC2_IP", "")
PR_URL = os.environ.get("PR_URL", "")
# Then validate and fail fast if required values are missing
```

**Why**: The cluster IP, org name, and project name change between deployments. Hardcoded values break other users and other environments silently.

**Exception**: `bhatti/*` GitHub repos (e.g. `bhatti/todo-sample`, `bhatti/you-got-skills`) are the author's own repos and are acceptable in tests and examples.

**Applies to**: ALL files — scripts, tests, YAML, docs, shell scripts.  
**Canonical env vars** (set in `~/.zshrc`):
- `EC2_IP` — EC2 host IP for Formicary
- `FORMICARY_URL` — Full URL override (default `https://$EC2_IP.nip.io`)
- `FORMICARY_TOKEN` — API bearer token
- `PR_URL` — Pull request URL for review/pr-comments tests
- `JIRA_BASE_URL`, `JIRA_PROJECT`, `JIRA_SPACE` — Jira config
- `GH_ORG`, `GH_REPO` — GitHub config
- `BITBUCKET_WORKSPACE`, `BITBUCKET_REPO` — Bitbucket config

---

### Rule #2: All scripts MUST produce output files in reports/

Every workflow script MUST write to `workspace/reports/`:
```
reports/result.json    — structured JSON result
reports/report.md      — Markdown report
reports/report.html    — HTML report
```

YAML artifact sections use `- ./reports` (directory), not individual files.

**Why**: Formicary UI can display any artifact; `./reports` as a directory means YAML never needs updating when new output files are added.

---

### Rule #3: Slack notification is mandatory on failure, optional on success

Every workflow that has a `SlackChannel` should:
- Always post on error (use `notify-error` task with `always_run: true`)
- Optionally post on success (use `slack_client.py` in the main task)

---

### Rule #4: _ensure_ygs_skills() before any _load_skill_md()

Scripts that call `_load_skill_md()` BEFORE `run_claude()` MUST call `_ensure_ygs_skills()` first:

```python
def main():
    _ensure_ygs_skills()   # ← FIRST, before any skill loading
    skill_md = _load_skill_md("ygs-review-pr")
    ...
    run_claude(...)
```

**Why**: `_ensure_ygs_skills()` is normally called inside `run_claude()`, but `_load_skill_md()` needs skills on disk BEFORE that call.

---

### Rule #5: Never use the Skill tool — embed SKILL.md directly

```python
# ❌ FORBIDDEN — depends on Claude Code's Skill tool being available
allowed_tools = "Bash,Read,Write,Skill"
prompt = "Invoke the /ygs-review-pr skill"

# ✅ CORRECT — read SKILL.md and embed in prompt
skill_md = _load_skill_md("ygs-review-pr")
prompt = f"Follow these instructions:\n\n{skill_md}\n\nApply to: {pr_url}"
allowed_tools = "Bash,Read,Write,Edit,Glob,Grep,LS"
```

**Why**: The Skill tool requires Claude Code's skill registry to be populated at runtime. Inside Formicary K8s pods, this is unreliable. Direct SKILL.md embedding always works.

---

## Build & Deploy

```bash
# Rebuild Docker image and push to Docker Hub (multi-arch amd64+arm64):
cd ~/workplace/ai-dev-tools
make docker-build

# Deploy workflow YAMLs to Formicary:
cd ~/workplace/formicary/docs/examples
source ~/.zshrc
./deploy-ai-jira-workflows.sh --set-configs
./deploy-ai-workflows.sh --set-configs

# Ant workers always pick up the latest image (image_pull_policy: Always).
# Restart Formicary queen only if config/binary changed:
EC2_IP=$EC2_IP ~/workplace/formicary/scripts/deploy-formicary.sh --restart
```

## Functional Tests

```bash
# Prerequisites: source ~/.zshrc (sets EC2_IP, FORMICARY_TOKEN, PR_URL, ...)
source ~/.zshrc

# Run first 2 tests (jira-query + analyze):
python3 tests/test_functional_workflows.py

# Run specific tests:
python3 tests/test_functional_workflows.py --tests jira-query,analyze,review

# List all tests:
python3 tests/test_functional_workflows.py --list
```

## Architecture Notes

- **Formicary overrides ENTRYPOINT**: `entrypoint.sh` never runs in task pods. All setup (YGS skills, `~/.claude/settings.json`) must happen inside Python scripts via `_ensure_ygs_skills()`.
- **Ant workers are ephemeral K8s pods**: No state persists between runs. `/workspace` is an emptyDir volume mounted per task.
- **`image_pull_policy: Always`**: New jobs automatically use the latest pushed Docker image.
- **Tailscale routing**: `http://ai/bedrock` is reachable from EC2 pods via Tailscale. Claude CLI hits Bedrock through this proxy.
- **Cron jobs MUST be triggered via the trigger API, never submitted fresh**: Jobs with `cron_trigger` (e.g. `ai-standup-jira`) have a PENDING scheduled request slot. Tests must call `trigger_cron_job()` (which hits `/api/v1/jobs/requests/:id/trigger`) to fire the pending slot immediately. Submitting a new request races with the cron schedule and causes duplicate/conflicting executions. In `TestCase`, set `cron=True` for any cron-based job type.
- **Adding extra skill repos**: Set `EXTRA_SKILLS_REPOS` env var to: (1) plain URL/name — `https://github.com/bhatti/you-got-skills.git`; (2) comma-separated — `url1,skills-cli:org/repo`; (3) JSON array (org config only — JSON breaks YAML template substitution). Auto-expands using `DEFAULT_TRACKER` + `BITBUCKET_WORKSPACE`/`GH_ORG`. Credentials auto-detected. Sparse checkout default (`sparse: true`). `skills-cli:` prefix or `"type": "skills-cli"` uses `npx skills add`. Skills dir auto-detected: `.claude/skills` → `skills` → `.skills`.
- **MAX_CLAUDE_PROCESS_TIMEOUT + process group kill**: Set to kill Claude after N seconds. Uses `os.killpg()` + `start_new_session=True` in Popen — `proc.kill()` alone does NOT work (Claude spawns children that hold stdout pipe open and block the drain loop forever). Stdout drain runs in a daemon thread with a `_ptimeout + 10s` deadline; after killpg fires it also closes `proc.stdout` to unblock the drain thread even if grandchildren survived. Critical: without this, stuck jobs hit 25m YAML task timeout instead of failing fast.
