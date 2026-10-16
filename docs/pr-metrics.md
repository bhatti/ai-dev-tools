# PR metrics

Every PR report (pr-risk, pr-audit, merge queue, open PRs, PR queue, PR review, standup) shows the same review-quality
metrics for GitHub and Bitbucket/Jira PRs, from one definition. Skill-side definition and rendering rules:
`you-got-skills/skills/shared/pr-metrics.md`.

| Metric | Meaning |
|--------|---------|
| Comments (human · bot) | Inline + top-level review comments and review summaries, deleted ones excluded |
| Tasks (human · bot, open) | Bitbucket PR tasks by creator; GitHub review threads by who started them; bots counted separately |
| Major/Critical issues | CRITICAL + HIGH from AI review findings, bot comments and human comments (`severity.py` markers), shown by source |
| Reviewers | Assigned + review authors + commenters minus the PR author; **bot reviewers that only commented count**; humans and bots separate |
| Commits | Commits excluding merge commits (2+ parents) so base-branch merges are not churn |

## Modules

| Module | Role |
|--------|------|
| `scripts/common/actors.py` | human / review bot / CI bot, from the tracker's account type (GitHub `Bot`, Bitbucket `app`) then names and body markers |
| `scripts/common/severity.py` | `count_findings`, `comment_severity`: the one definition of "major/critical" |
| `scripts/common/pr_signals/` | fetches the metrics: `github.py` (batched GraphQL), `bitbucket.py` (REST, conditional calls), `retry.py` (429/5xx backoff), per-run cache `pr_signals.json` |
| `scripts/common/pr_metrics.py` | the metric registry; `build_pr_table` (per-PR table), `build_single_pr_metrics` (the one single-PR table), `aggregate_rows` (dashboard rows), `enrich_with_signals` |
| `scripts/mq/gate_check.py` | gate decision from `risk_score.json` + `reports/findings.json` |

Adding a metric is one `Metric` entry in `pr_metrics.REGISTRY` (cell, row, signal group); every table, the fetch and the docs check follow.

## One table per report

Single-PR reports (`pr-risk`, review) show one `PR Metrics` table (`Metric | Value | Benchmark | Signal | Description`).
Multi-PR reports show one per-PR table plus the one aggregate `Metrics Dashboard`. `—` means not collected, `n/a` means the
fetch failed (never zero), a trailing `+` means the fetch cap was hit.

## Cost and tuning

- GitHub: one GraphQL request per 10 PRs (not one call per PR). Token needs no org scope.
- Bitbucket: per PR one small call, then `/comments` (only when `comment_count` is non-zero), `/tasks` and `/commits` (whenever the group is requested); pooled with `DIFFSTAT_WORKERS` (default 8, max 20); `fields=` partial responses.
- Only the groups a report shows are fetched (`needs_for(columns)`); PR audit reuses the comments it already fetched.
- 429/5xx are retried honouring `Retry-After`; a failure becomes an explicit `n/a`.
- `PR_SIGNALS=off` disables the fetch; `BITBUCKET_API_BASE` points the Bitbucket client at a proxy.
- Inspect a PR: `python3 -m scripts.common.pr_signals --tracker github 24`.

## Tests

Unit: `test_actors.py`, `test_severity.py`, `test_pr_signals.py`, `test_pr_metrics.py`, `test_mq_gate_check.py`.
Integration (`tests/integration/`): real entry points in subprocesses against a fake `gh` executable and a local Bitbucket HTTP server (`tests/fakes/`), the same review story on both trackers must give the same metrics. Pod: `pr-signals-gh`, `pr-signals-bb`, `open-prs-metrics-gh`, `pr-risk-gh|bb`.
