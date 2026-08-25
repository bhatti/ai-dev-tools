# Architecture

## Overview

ai-dev-tools is a set of small, independent Python scripts packaged in a single Docker image. Each script performs one step of the AI coding workflow and communicates with adjacent steps via files on a shared volume.

## Design Principles

1. **Small scripts** — each script does one thing and fits in a few screens
2. **Idempotent** — every script checks if its output already exists; re-running is safe
3. **File-based handoff** — scripts communicate via `/workspace/{issue-id}/` JSON and Markdown files, not env vars or queues
4. **Exit codes as contracts** — `0`=success, `1`=error (retryable), `2`=blocked (needs human)
5. **No framework dependency at runtime** — the scripts work independently; K8s is just the orchestrator

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│  Docker Image: ghcr.io/bhatti/ai-dev-tools                          │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  scripts/common/                                              │   │
│  │    config.py       — env var loading + defaults               │   │
│  │    artifacts.py    — read/write /workspace/{id}/              │   │
│  │    git_utils.py    — clone, branch, commit, push              │   │
│  │    claude_runner.py — invoke `claude` CLI                     │   │
│  │    label_utils.py   — GH labels, Jira labels                  │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  ┌────────────────────┐    ┌────────────────────────────┐           │
│  │  scripts/gh/       │    │  scripts/jira/             │           │
│  │    issue_picker.py │    │    issue_picker.py         │           │
│  │    plan.py         │    │    plan.py                 │           │
│  │    implement.py    │    │    implement.py            │           │
│  │    create_pr.py    │    │    create_pr.py            │           │
│  │    monitor_pr.py   │    │    monitor_pr.py           │           │
│  │    learn.py        │    │    learn.py                │           │
│  └────────────────────┘    └────────────────────────────┘           │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────┐     │
│  │  scripts/review/                                            │     │
│  │    run.py           — invoke ygs-review-pr skill via Claude │     │
│  │    post_findings.py — post Block Kit findings to Slack      │     │
│  │                       always exits 3 → PAUSE_JOB           │     │
│  │    apply_feedback.py — post decision confirmation to Slack  │     │
│  └────────────────────────────────────────────────────────────┘     │
│                                                                      │
│  ┌──────────────────────────────────────────────────────┐           │
│  │  scripts/adhoc/                                       │           │
│  │    run_skill.py — run any you-got-skills skill,       │           │
│  │                   post result to Slack thread         │           │
│  └──────────────────────────────────────────────────────┘           │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  scripts/slack/                                               │   │
│  │    router.py          — Bolt Socket Mode app                  │   │
│  │    formicary_client.py — submit / find_jobs / resume          │   │
│  │    registry.py        — resolve intent → job_type             │   │
│  │    workflows.yml      — declarative workflow registry         │   │
│  │    skills.yml         — declarative skill registry            │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  Tools: claude CLI, codex CLI, gh CLI, acli, git                    │
└─────────────────────────────────────────────────────────────────────┘
```

## Artifact Flow

```
/workspace/
└── {issue-id}/
    ├── issue.json          ← written by issue_picker
    ├── plan.md             ← written by plan (Claude output)
    ├── plan_result.json    ← {"status":"DONE","task_count":3,...}
    ├── branch.txt          ← branch name (written by implement)
    ├── impl_result.json    ← {"status":"DONE","commits":5,...}
    ├── pr.json             ← {"url":"...","number":123,...}
    ├── monitor_result.json ← {"status":"MERGED"}
    ├── processed_comments.json ← {"ids":[1,2,3]}
    ├── learnings.md        ← written by learn
    ├── PLANS/              ← plan files written by Claude
    ├── repo/               ← cloned git repository
    └── logs/
        ├── plan.log
        ├── implement.log
        ├── create_pr.log
        └── feedback_{id}.log
```

## Kubernetes Job Pattern

The pipeline uses K8s init containers for sequential steps:

```
Pod lifecycle:
  initContainer: plan       → must exit 0 before next starts
  initContainer: implement  → must exit 0 before next starts
  initContainer: create_pr  → must exit 0 before next starts
  container:     monitor_pr → long-running polling loop
```

The init-container pattern gives us:
- Natural sequential execution
- K8s restart/retry semantics per step
- Crash isolation (a failing step stops the pipeline cleanly)

## Claude Integration

Scripts invoke Claude Code via the `claude` CLI:

```bash
claude --print \
       --dangerously-skip-permissions \
       --model claude-sonnet-4-6 \
       --max-turns 30 \
       "<prompt>"
```

Claude Code has its own tool-use loop — it can read files, write code, run commands, and make commits inside the repo directory. The scripts pass the working directory as the Claude working dir so Claude operates within the cloned repository.

Status is extracted from Claude's output by finding the last JSON object containing `"status"` key:

```python
re.findall(r'\{[^{}]*"status"[^{}]*\}', output)[-1]
```

## Idempotency

Each script follows the same pattern:

```python
existing = read_json(config, issue_id, "THIS_step_result.json")
if existing and existing.get("status") == "DONE":
    print("Already done, skipping")
    sys.exit(0)
```

This means:
- Crashing mid-step and re-running is safe
- The K8s Job `backoffLimit: 1` retries once on exit code 1
- Manual re-runs during debugging are safe

## Slack Router

The Slack agent router (`scripts/slack/router.py`) is a long-running Bolt Socket Mode app deployed as a K8s Deployment (1 replica, `Recreate` strategy). It requires no public ingress — Socket Mode uses an outbound WebSocket to Slack's servers.

For the full explanation see **[docs/slack-router.md](slack-router.md)**.

### Summary

1. User `@bot <command>` in Slack
2. Router strips mention, normalises Slack links, resolves intent via verb parse or Haiku LLM
3. Looks up `job_type` in `workflows.yml` registry
4. Submits Formicary job with `SlackThreadTs` so the skill can reply in-thread
5. Skill posts result back via `notify(config, text, blocks=blocks)` — Block Kit structured output with plain-text fallback

### Extension point

`scripts/slack/workflows.yml` and `scripts/slack/skills.yml` are the sole extension points. Adding a new skill requires one YAML entry — no code changes to the router.

### Available commands

| Command | Job type | Description |
|---------|----------|-------------|
| `standup` | `ai-standup-jira` / `ai-standup-gh` | Daily standup brief |
| `risk` | `ai-adhoc` + `ygs-risk-scan` | Sprint risk scan |
| `prs` / `pr queue` | `ai-adhoc` + `ygs-pr-queue` | Open PR queue |
| `review <url>` | `ai-gh-review` / `ai-jira-review` | AI code review + pause for decision |
| `implement <id>` | `ai-jira-implement` / `ai-gh-implement` | Full implement pipeline |
| `qjira <term>` | `ai-jira-query` | Search open Jira issues by keyword |
| `jira-analyze <keys>` | `ai-jira-query` (Mode=analyze) | Root-cause analysis for issues |
| `pr comments <url>` | `ai-adhoc` | Inline comments + tasks for a PR |
| `security review` / `sre review` | `ai-gh-review` | Specialist PR reviews |
| `help` | — | List all commands |

---

## PR Review Flow

```
Slack: @bot review https://github.com/org/repo/pull/42
  └─→ router submits ai-gh-review with PRUrl + SlackThreadTs

Formicary:
  [review task]        scripts.review.run
                         └─→ Claude invokes /ygs-review-pr
                         └─→ writes findings.json
  [await-feedback]     scripts.review.post_findings
                         └─→ posts Block Kit to Slack (exits 3 → PAUSE_JOB)
  ── JOB PAUSED ──
  Human clicks "Approve" or "Request Changes" in Slack
  └─→ router: GET job → merge Decision var → PUT → POST /trigger
  ── JOB RESUMES ──
  [finalize task]      scripts.review.apply_feedback
                         └─→ reads Decision env var
                         └─→ posts confirmation to Slack thread
  [done]
```

---

## Exit Code Contract

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Error (retryable) |
| `2` | Blocked (needs human — not used in review flow) |
| `3` | PAUSE_JOB — `post_findings.py` always exits 3 |

---

## Security

- Secrets are in K8s Secrets, not ConfigMaps or env files
- SSH key mounted at `/secrets/ssh-key` with mode 0600
- `gh auth login` is called in entrypoint to authenticate the `gh` CLI
- Claude runs with `--dangerously-skip-permissions` inside the container (no interactive prompts)
- The container runs as non-root (`agent` user, uid 1000) in production K8s deployments

---

## Further Reading

- [system-reference.md](system-reference.md) — complete operational guide: all job types, config system, Slack router, skills integration, all gotchas
- [slack-router.md](slack-router.md) — Slack router deep-dive: request flow, thread replies, registry, Block Kit output
- [configuration.md](configuration.md) — all env vars, org configs, K8s secret reference
