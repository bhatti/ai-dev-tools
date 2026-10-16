"""PR review report end to end: scripts.review.post_findings reads findings.json and the PR's review activity."""

from __future__ import annotations

import pytest

from tests.fakes.bitbucket import FakeBitbucket
from tests.fakes.gh import FakeGh
from tests.fakes.stories import bb_story, gh_node, gh_response
from tests.integration.pipeline import base_env, metrics_rows, run_module, workspace, write_json
from tests.integration.test_pr_risk_pipeline import FINDINGS

URLS = {"github": "https://github.com/acme/widgets/pull/7", "bitbucket": "https://bitbucket.org/ws/repo/pull-requests/7"}


@pytest.mark.parametrize("tracker", ["github", "bitbucket"])
def test_review_report_has_the_one_metrics_table(tracker, tmp_path, monkeypatch):
    findings = {**FINDINGS, "pr_url": URLS[tracker]}
    if tracker == "github":
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node(7)))
        env = base_env(tmp_path, tracker)
        report = _run(env, findings)
    else:
        with FakeBitbucket(monkeypatch) as bb:
            bb_story(bb, 7)
            env = base_env(tmp_path, tracker, {"BITBUCKET_API_BASE": bb.base})
            report = _run(env, findings)
    assert report.count("| Metric | Value | Benchmark | Signal | Description |") == 1
    rows = metrics_rows(report)
    assert rows["Comments (human · bot)"][1] == "3 👤 · 3 🤖"
    assert rows["Tasks (human · bot)"][1] == "1 👤 · 1 🤖 (1 open)"
    assert rows["Commits"][1] == "2"
    assert rows["Major/Critical Issues"][1] == "3 (ai 2 · bot 1)"
    assert "## Findings" in report and "### 🔴 CRITICAL" in report            # the review itself is unchanged


def test_review_report_still_renders_when_the_pr_activity_cannot_be_read(tmp_path, monkeypatch):
    FakeGh(tmp_path, monkeypatch).on("graphql", "", rc=1, stderr="boom")
    env = base_env(tmp_path, "github")
    report = _run(env, {**FINDINGS, "pr_url": URLS["github"]})
    rows = metrics_rows(report)
    assert rows["Comments (human · bot)"][1] == "n/a" and rows["Major/Critical Issues"][1] == "2 (ai 2)"


def _run(env, findings) -> str:
    write_json(workspace(env) / "reports" / "findings.json", findings)
    proc = run_module("scripts.review.post_findings", env, "--findings", str(workspace(env) / "reports" / "findings.json"))
    assert proc.returncode == 0, proc.stderr[-800:]
    return (workspace(env) / "reports" / "report.md").read_text()
