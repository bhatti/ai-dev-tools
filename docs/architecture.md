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
┌─────────────────────────────────────────────────────────────┐
│  Docker Image: ghcr.io/bhatti/ai-dev-tools                  │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  scripts/common/                                      │   │
│  │    config.py     — env var loading + defaults         │   │
│  │    artifacts.py  — read/write /workspace/{id}/        │   │
│  │    git_utils.py  — clone, branch, commit, push        │   │
│  │    claude_runner.py — invoke `claude` CLI             │   │
│  │    label_utils.py   — GH labels, Jira labels          │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌────────────────────┐    ┌────────────────────────────┐   │
│  │  scripts/gh/       │    │  scripts/jira/             │   │
│  │    issue_picker.py │    │    issue_picker.py         │   │
│  │    plan.py         │    │    plan.py                 │   │
│  │    implement.py    │    │    implement.py            │   │
│  │    create_pr.py    │    │    create_pr.py            │   │
│  │    monitor_pr.py   │    │    monitor_pr.py           │   │
│  │    learn.py        │    │    learn.py                │   │
│  └────────────────────┘    └────────────────────────────┘   │
│                                                              │
│  Tools: claude CLI, codex CLI, gh CLI, acli, git            │
└─────────────────────────────────────────────────────────────┘
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

## Security

- Secrets are in K8s Secrets, not ConfigMaps or env files
- SSH key mounted at `/secrets/ssh-key` with mode 0600
- `gh auth login` is called in entrypoint to authenticate the `gh` CLI
- Claude runs with `--dangerously-skip-permissions` inside the container (no interactive prompts)
- The container runs as root (Alpine default) — consider adding a non-root user for production
