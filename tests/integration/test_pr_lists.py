"""Multi-PR reports end to end (open PRs, merge queue, PR queue), GitHub and Bitbucket, with the review metrics columns.

Real entry points in subprocesses; GitHub via the fake `gh`, Bitbucket via the fake REST server.
"""

from __future__ import annotations

import pytest

from tests.fakes.bitbucket import FakeBitbucket
from tests.fakes.gh import FakeGh
from tests.fakes.stories import bb_story, gh_node, gh_response
from tests.integration.pipeline import base_env, run_module, workspace

GH_LIST = [{"number": 7, "title": "Add cart limit", "author": {"login": "alice"}, "createdAt": "2026-09-25T10:00:00Z",
            "reviews": [{"state": "APPROVED", "author": {"login": "bob"}}], "reviewRequests": [{"login": "carol"}],
            "url": "https://github.com/acme/widgets/pull/7", "headRefName": "feat/cart", "baseRefName": "main", "labels": [],
            "statusCheckRollup": [{"conclusion": "SUCCESS", "status": "COMPLETED"}]}]


def bb_list_routes(bb: FakeBitbucket) -> None:
    bb_story(bb, 7)
    bb.add_pages("/repositories/ws/repo/pullrequests", [{"values": [{
        "id": 7, "title": "Add cart limit", "description": "", "author": {"display_name": "alice"}, "created_on": "2026-09-25T10:00:00+00:00",
        "source": {"branch": {"name": "feat/cart"}}, "destination": {"branch": {"name": "main"}}, "reviewers": [{"display_name": "carol"}],
        "links": {"html": {"href": "https://bitbucket.org/ws/repo/pull-requests/7"}}}]}])
    pr = bb.routes["/2.0/repositories/ws/repo/pullrequests/7"][0]
    pr.update({"id": 7, "title": "Add cart limit", "state": "OPEN", "created_on": "2026-09-25T10:00:00+00:00",
               "source": {"branch": {"name": "feat/cart"}}, "destination": {"branch": {"name": "main"}}})
    bb.add_pages("/repositories/ws/repo/pullrequests/7/diffstat",
                 [{"values": [{"new": {"path": "api/cart.py"}, "lines_added": 40, "lines_removed": 5}]}])


def row_for(report: str, header_cell: str) -> dict[str, str]:
    """First data row of the per-PR table that has a ``header_cell`` column, as {header: cell}."""
    lines = report.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("|") and f" {header_cell} " in line and "Comments" in line:
            headers = [c.strip() for c in line.strip().strip("|").split("|")]
            cells = [c.strip() for c in lines[i + 2].strip().strip("|").split("|")]
            return dict(zip(headers, cells))
    raise AssertionError(f"no per-PR table with a {header_cell!r} column in:\n{report[:1500]}")


@pytest.fixture
def github(tmp_path, monkeypatch):
    gh = (FakeGh(tmp_path, monkeypatch).on("pr list", GH_LIST).on("graphql", gh_response(gh_node(7)))
          .on("--json files", {"files": [{"path": "api/cart.py", "additions": 40, "deletions": 5}], "additions": 40, "deletions": 5})
          .on("/files", "[]"))
    return gh, base_env(tmp_path, "github")


@pytest.fixture
def bitbucket(tmp_path, monkeypatch):
    with FakeBitbucket(monkeypatch) as bb:
        bb_list_routes(bb)
        yield bb, base_env(tmp_path, "bitbucket", {"BITBUCKET_API_BASE": bb.base})


EXPECTED_CELLS = {"Comments": "3 👤 · 3 🤖", "Tasks": "1 👤 · 1 🤖 (1 open)", "Commits": "2", "Major/Critical": "1 (bot 1)", "Reviewers": "1/2 ✅ · 2 🤖"}


@pytest.mark.parametrize("tracker", ["github", "bitbucket"])
def test_open_prs_report_has_one_per_pr_table_with_review_metrics(tracker, github, bitbucket):
    fake, env = github if tracker == "github" else bitbucket
    proc = run_module("scripts.analyze.run_open_prs", env)
    assert proc.returncode == 0, proc.stderr[-1000:]
    report = (workspace(env) / "reports" / "open_prs_report.md").read_text()
    row = row_for(report, "Reviewers")
    for column, expected in EXPECTED_CELLS.items():
        assert row[column] == expected, (column, row)
    assert report.count("### Metrics Dashboard") == 1
    assert "| PRs without a human comment | 0/1 (0%) |" in report


def test_github_open_prs_reads_all_review_activity_in_one_request(github):
    fake, env = github
    assert run_module("scripts.analyze.run_open_prs", env).returncode == 0
    assert len(fake.calls_matching("graphql")) == 1


def test_open_prs_still_renders_when_review_signals_are_unavailable(tmp_path, monkeypatch):
    FakeGh(tmp_path, monkeypatch).on("pr list", GH_LIST).on("graphql", "", rc=1, stderr="boom").on("--json files", {"files": []}).on("/files", "[]")
    env = base_env(tmp_path, "github")
    proc = run_module("scripts.analyze.run_open_prs", env)
    assert proc.returncode == 0
    row = row_for((workspace(env) / "reports" / "open_prs_report.md").read_text(), "Reviewers")
    assert row["Comments"] == "n/a" and row["Commits"] == "n/a"          # unavailable is never shown as 0


def test_pr_signals_can_be_switched_off(github):
    fake, env = github
    env["PR_SIGNALS"] = "off"
    proc = run_module("scripts.analyze.run_open_prs", env)
    assert proc.returncode == 0 and not fake.calls_matching("graphql")
    assert row_for((workspace(env) / "reports" / "open_prs_report.md").read_text(), "Reviewers")["Comments"] == "—"


@pytest.mark.parametrize("tracker", ["github", "bitbucket"])
def test_merge_queue_lanes_show_the_review_metrics(tracker, github, bitbucket):
    fake, env = github if tracker == "github" else bitbucket
    env["REPORT_TITLE"] = "Merge Queue"
    for module in ("scripts.mq.collect_ready", "scripts.mq.group_by_scope", "scripts.mq.report"):
        proc = run_module(module, env)
        assert proc.returncode == 0, f"{module}: {proc.stderr[-800:]}"
    report = (workspace(env) / "reports" / "report.md").read_text()
    row = row_for(report, "Reviewers")
    for column, expected in EXPECTED_CELLS.items():
        assert row[column] == expected, (column, row)


def test_pr_queue_table_shows_the_review_metrics_and_bot_reviewers(github):
    import json
    from scripts.adhoc.run_skill import _pr_queue_to_markdown
    fake, env = github
    proc = run_module("scripts.standup.gather_pr_queue", env)
    assert proc.returncode == 0, proc.stderr[-800:]
    data = json.loads((workspace(env) / "pr_queue.json").read_text())
    assert data["prs"][0]["signals"]["comments_bot"] == 3
    md = _pr_queue_to_markdown(data, "PR Queue")
    row = row_for(md, "Reviewers")
    assert row["Comments"] == "3 👤 · 3 🤖" and row["Tasks"] == "1 👤 · 1 🤖 (1 open)" and row["Commits"] == "2"
    assert "🤖 coderabbitai[bot]" in row["Reviewers"]           # bot reviewer that only commented
    assert len(fake.calls_matching("graphql")) == 1
