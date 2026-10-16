"""PR-risk chain end to end: scope_router -> risk_score -> mq.report -> gate_check, for GitHub and Bitbucket (Jira).

Each stage is the real entry point run in a subprocess. GitHub talks to a fake `gh` executable, Bitbucket to a local
HTTP server through BITBUCKET_API_BASE. The same review story is served by both, so the metrics tables must match.
"""

from __future__ import annotations

import json

import pytest

from tests.fakes.bitbucket import FakeBitbucket
from tests.fakes.gh import FakeGh
from tests.fakes.stories import bb_story, gh_node, gh_response
from tests.integration.pipeline import base_env, metrics_rows, run_module, workspace, write_json

FILES = [{"path": "api/cart.py", "additions": 40, "deletions": 5}, {"path": "api/limits.py", "additions": 10, "deletions": 0}]
FINDINGS = {"verdict": "REQUEST_CHANGES", "summary": "limit check is racy", "findings": [
    {"severity": "CRITICAL", "title": "race", "file": "api/cart.py", "line": 12, "description": "d", "fix": "f"},
    {"severity": "HIGH", "title": "no test", "file": "api/cart.py", "line": 30, "description": "d", "fix": "f"},
    {"severity": "LOW", "title": "nit", "file": "api/limits.py", "line": 1, "description": "d", "fix": "f"}]}


def gh_pr_view(number=7):
    return {"number": number, "title": "Add cart limit", "body": "Fixes #3", "author": {"login": "alice"},
            "createdAt": "2026-09-25T10:00:00Z", "mergedAt": None, "state": "OPEN", "url": f"https://github.com/acme/widgets/pull/{number}",
            "headRefName": "feat/cart", "comments": [], "reviews": [], "reviewDecision": "", "labels": [], "files": FILES,
            "additions": 50, "deletions": 5}


def install_github(tmp_path, monkeypatch) -> FakeGh:
    return (FakeGh(tmp_path, monkeypatch)
            .on("--json files,additions,deletions", {"files": FILES, "additions": 50, "deletions": 5})
            .on("graphql", gh_response(gh_node(7)))
            .on("/comments --paginate", "[]")
            .on("pr view", gh_pr_view()))


def install_bitbucket(bb: FakeBitbucket) -> None:
    bb_story(bb, 7)
    base = "/repositories/ws/repo/pullrequests/7"
    pr = bb.routes[f"/2.0{base}"][0]
    pr.update({"id": 7, "title": "Add cart limit", "description": "Fixes ABC-3", "state": "OPEN", "created_on": "2026-09-25T10:00:00+00:00",
               "source": {"branch": {"name": "feat/cart"}}, "destination": {"branch": {"name": "main"}},
               "links": {"html": {"href": "https://bitbucket.org/ws/repo/pull-requests/7"}}})
    pr["author"] = {"display_name": "alice", "account_type": "personal"}
    bb.add_pages(f"{base}/diffstat", [{"values": [{"new": {"path": f["path"]}, "lines_added": f["additions"], "lines_removed": f["deletions"]} for f in FILES]}])


def run_chain(tmp_path, env) -> tuple[str, dict]:
    for module, args in (("scripts.mq.scope_router", ("--pr-number", "7")), ("scripts.mq.risk_score", ("--pr-number", "7"))):
        proc = run_module(module, env, *args)
        assert proc.returncode == 0, f"{module}: {proc.stderr[-800:]}{proc.stdout[-400:]}"
    write_json(workspace(env) / "reports" / "findings.json", FINDINGS)
    proc = run_module("scripts.mq.report", {**env, "PR_NUMBER": "7", "REPORT_TITLE": "PR risk"})
    assert proc.returncode == 0, proc.stderr[-800:]
    report = (workspace(env) / "reports" / "report.md").read_text()
    proc = run_module("scripts.mq.gate_check", {**env, "APPROVAL_THRESHOLD": "70"})
    assert proc.returncode == 0, proc.stderr[-800:]
    return report, json.loads((workspace(env) / "gate_result.json").read_text())


@pytest.fixture
def github_run(tmp_path, monkeypatch):
    gh = install_github(tmp_path, monkeypatch)
    report, gate = run_chain(tmp_path, base_env(tmp_path, "github"))
    return report, gate, gh


@pytest.fixture
def bitbucket_run(tmp_path, monkeypatch):
    with FakeBitbucket(monkeypatch) as bb:
        install_bitbucket(bb)
        report, gate = run_chain(tmp_path, base_env(tmp_path, "bitbucket", {"BITBUCKET_API_BASE": bb.base}))
        yield report, gate, bb


def test_github_report_has_one_metrics_table_with_the_review_activity(github_run):
    report, gate, _ = github_run
    assert report.count("| Metric | Value | Benchmark | Signal | Description |") == 1
    assert "## Scope" not in report and "## Metrics Dashboard" not in report
    rows = metrics_rows(report)
    assert rows["Comments (human · bot)"][1] == "3 👤 · 3 🤖"
    assert rows["Tasks (human · bot)"][1] == "1 👤 · 1 🤖 (1 open)"
    assert rows["Commits"][1] == "2"
    assert rows["Major/Critical Issues"][1] == "3 (ai 2 · bot 1)"
    assert rows["Reviewers"][1] == "1/2 ✅ · 2 🤖"


def test_gate_decision_counts_come_from_the_real_findings(github_run):
    _, gate, _ = github_run
    assert gate["needs_approval"] is True and gate["has_critical_findings"] is True
    assert (gate["findings_count"], gate["critical_count"], gate["high_count"]) == (3, 1, 1)


def test_bitbucket_and_github_report_the_same_review_metrics(github_run, bitbucket_run):
    gh_rows, bb_rows = metrics_rows(github_run[0]), metrics_rows(bitbucket_run[0])
    for metric in ("Comments (human · bot)", "Tasks (human · bot)", "Commits", "Reviewers", "Major/Critical Issues", "LOC Changed", "Files Changed"):
        assert gh_rows[metric][1:3] == bb_rows[metric][1:3], metric


def test_review_activity_is_fetched_once_for_the_whole_chain(github_run):
    _, _, gh = github_run
    assert len(gh.calls_matching("graphql")) == 1       # scope_router fetched it; the report read the cache


def test_scope_router_uses_the_trackers_of_the_prs_own_repo_not_the_default(tmp_path, monkeypatch):
    """DEFAULT_TRACKER=jira (Bitbucket) but the PR is a GitHub URL: review activity must come from GitHub."""
    gh = install_github(tmp_path, monkeypatch)
    env = base_env(tmp_path, "bitbucket", {"BITBUCKET_API_BASE": "http://127.0.0.1:9/2.0"})   # nothing listens: any Bitbucket call would fail
    proc = run_module("scripts.mq.scope_router", env, "--pr-number", "https://github.com/acme/widgets/pull/7")
    assert proc.returncode == 0, proc.stderr[-600:]
    cache = json.loads((workspace(env) / "pr_signals.json").read_text())
    assert cache["tracker"] == "github" and cache["prs"]["7"]["comments_human"] == 3 and not cache["prs"]["7"].get("error")
    assert len(gh.calls_matching("graphql")) == 1
