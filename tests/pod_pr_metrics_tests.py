"""Pod functional tests for the shared PR metrics (scripts/common/pr_signals, pr_metrics, severity, actors).

Real scripts in real pods (local scripts copied over the image) against the real trackers, no LLM:

    PYTHONPATH=. python3 tests/test_pod_functional.py --tests pr-signals-gh,pr-signals-bb,open-prs-metrics-gh

pr-signals-gh         real `gh` GraphQL: one batched request returns comments/reviewers/tasks/commits for a sandbox PR
pr-signals-bb         real Bitbucket REST: a missing PR is reported as an explicit error (read-only), or a real PR when
                      QA_TEST_BB_PR_URL is set
open-prs-metrics-gh   scripts.analyze.run_open_prs against the sandbox repo: one per-PR table with the new columns, one dashboard
"""

from __future__ import annotations

import json
import os
import re
from types import ModuleType

PR_METRICS_SCRIPTS = [
    "scripts/common/actors.py", "scripts/common/severity.py", "scripts/common/pr_metrics.py", "scripts/common/pr_classify.py",
    "scripts/common/pr_metadata.py", "scripts/common/bitbucket_api.py", "scripts/common/config.py", "scripts/common/models.py",
    "scripts/common/models.json", "scripts/common/pr_signals/__init__.py", "scripts/common/pr_signals/__main__.py",
    "scripts/common/pr_signals/github.py", "scripts/common/pr_signals/bitbucket.py", "scripts/common/http_retry.py",
    "scripts/analyze/pr_fetcher.py", "scripts/analyze/run_open_prs.py", "scripts/analyze/run_pr_audit.py",
    "scripts/mq/_shared.py", "scripts/mq/scope_router.py", "scripts/mq/gate_check.py", "scripts/mq/report.py", "scripts/mq/collect_ready.py",
    "scripts/review/post_findings.py", "scripts/standup/gather_pr_queue.py", "scripts/standup/gather_gh.py", "scripts/standup/gather_jira.py",
    "scripts/standup/slack_client.py", "scripts/adhoc/run_skill.py",
]
GH_ORG = os.environ.get("GH_ORG", "bhatti")
GH_REPO = os.environ.get("GH_REPO", "todo-sample")
GH_PR = int(re.search(r"/pull/(\d+)", os.environ.get("QA_TEST_GH_PR_URL", "https://github.com/bhatti/todo-sample/pull/24")).group(1))
BB_PR_URL = os.environ.get("QA_TEST_BB_PR_URL", "")


def build_tests(h: ModuleType) -> dict:
    for rel in PR_METRICS_SCRIPTS:
        if rel not in h._SCRIPTS_TO_COPY:
            h._SCRIPTS_TO_COPY.append(rel)

    def gh_env(base_env, ws):
        env = {k: v for k, v in base_env.items() if not k.startswith(("JIRA_", "BITBUCKET_"))}
        env.update({"GH_ORG": GH_ORG, "GH_REPO": GH_REPO, "DEFAULT_TRACKER": "github", "WORKSPACE_DIR": ws, "SLACK_BOT_TOKEN": ""})
        return env

    def test_signals_gh(base_env):
        result = h.TestResult("pr-signals-gh")
        ws = "/workspace/pr_signals_gh"
        with h.pod_fixture("pr-signals-gh") as pod:
            step = h.exec_step(pod, "signals", f"mkdir -p {ws}\npython3 -m scripts.common.pr_signals --tracker github {GH_PR}", gh_env(base_env, ws), timeout=180)
            result.steps.append(step)
            if not step.ok:
                return h._fail(result, step, f"exit {step.returncode}: {step.stderr[-400:]}")
            try:
                rec = json.loads(step.stdout[step.stdout.index("{"):])[str(GH_PR)]
            except (ValueError, KeyError) as e:
                return h._fail(result, step, f"output is not the expected JSON ({e}): {step.stdout[-300:]}")
            missing = [k for k in ("comments_human", "comments_bot", "reviewers_human", "reviewers_bot", "tasks_human", "tasks_bot", "tasks_unresolved", "commits")
                       if not isinstance(rec.get(k), int)]
            if rec.get("error") or missing:
                return h._fail(result, step, f"record incomplete: error={rec.get('error')!r} missing={missing}")
            second = h.exec_step(pod, "signals-cached", f"python3 -m scripts.common.pr_signals --tracker github {GH_PR} && test -s {ws}/pr_signals.json", gh_env(base_env, ws), timeout=60)
            result.steps.append(second)
            if not second.ok:
                return h._fail(result, second, "second call must succeed and leave the per-run cache file")
        return h._pass(result, f"PR #{GH_PR}: comments {rec['comments_human']}h/{rec['comments_bot']}b, tasks {rec['tasks_human']}h/{rec['tasks_bot']}b, commits {rec['commits']}")

    def test_signals_bb(base_env):
        result = h.TestResult("pr-signals-bb")
        ws = "/workspace/pr_signals_bb"
        env = {k: v for k, v in base_env.items() if not k.startswith("GH_")}
        env.update({"DEFAULT_TRACKER": "jira", "WORKSPACE_DIR": ws})
        if BB_PR_URL:
            m = re.search(r"bitbucket\.org/([^/]+)/([^/]+)/pull-requests/(\d+)", BB_PR_URL)
            env.update({"BITBUCKET_WORKSPACE": m.group(1), "BITBUCKET_REPO": m.group(2)})
            number = m.group(3)
        else:
            env.update({"BITBUCKET_WORKSPACE": env.get("BITBUCKET_WORKSPACE", "example-org"), "BITBUCKET_REPO": env.get("BITBUCKET_REPO", "example-repo")})
            number = "99999999"
        with h.pod_fixture("pr-signals-bb") as pod:
            step = h.exec_step(pod, "signals", f"mkdir -p {ws}\npython3 -m scripts.common.pr_signals --tracker bitbucket {number}; echo \"::add-task-context RC::$?\"", env, timeout=180)
            result.steps.append(step)
            try:
                rec = json.loads(step.stdout[step.stdout.index("{"):step.stdout.rindex("}") + 1])[number]
            except (ValueError, KeyError) as e:
                return h._fail(result, step, f"output is not the expected JSON ({e}): {step.stdout[-300:]}")
            if BB_PR_URL:
                if step.context.get("RC") != "0" or rec.get("error") or not isinstance(rec.get("comments_human"), int):
                    return h._fail(result, step, f"real PR must give a complete record: {rec}")
            elif rec.get("error") not in ("not found",) and "401" not in str(rec.get("error")) and "403" not in str(rec.get("error")):
                return h._fail(result, step, f"a missing PR must be an explicit error, got {rec}")
        return h._pass(result, "real Bitbucket REST: " + ("PR record complete" if BB_PR_URL else f"missing PR reported explicitly ({rec['error']})"))

    def test_open_prs_gh(base_env):
        result = h.TestResult("open-prs-metrics-gh")
        ws = "/workspace/open_prs_metrics_gh"
        with h.pod_fixture("open-prs-metrics") as pod:
            step = h.exec_step(pod, "open-prs", f"mkdir -p {ws}/reports\npython3 -m scripts.analyze.run_open_prs\ncat {ws}/reports/open_prs_report.md", gh_env(base_env, ws), timeout=300)
            result.steps.append(step)
            if not step.ok:
                return h._fail(result, step, f"exit {step.returncode}: {step.stderr[-400:]}")
            out = step.stdout
            if "No open PRs found" in out:
                return h._pass(result, "sandbox repo has no open PRs: report rendered (columns not exercised)")
            for column in ("| Comments |", "| Tasks |", "| Major/Critical |", "| Commits |"):
                if column not in out:
                    return h._fail(result, step, f"per-PR table lacks the {column.strip('| ')} column")
            if out.count("### Metrics Dashboard") != 1:
                return h._fail(result, step, f"expected exactly one Metrics Dashboard, got {out.count('### Metrics Dashboard')}")
        return h._pass(result, "per-PR table has the review-metric columns; exactly one dashboard")

    return {"pr-signals-gh": test_signals_gh, "pr-signals-bb": test_signals_bb, "open-prs-metrics-gh": test_open_prs_gh}
