"""PR audit metrics assembly: comment-derived signals + batched tasks/commits, merged into the single dashboard.

The audit's clone and model call are covered by the pod tests; here the real adapters run against the fake `gh` /
Bitbucket server and the real report-assembly functions build the tables.
"""

from __future__ import annotations

import pytest

from scripts.analyze.run_pr_audit import _attach_review_signals, _build_metrics_summary_with_claude
from scripts.common.pr_classify import extract_and_strip_metrics_tables
from scripts.analyze.pr_fetcher import classify_comments
from scripts.common.pr_metrics import TABLE_COLUMNS
from tests.fakes.bitbucket import FakeBitbucket
from tests.fakes.gh import FakeGh
from tests.fakes.stories import bb_story, gh_node, gh_response

CFG = {"github": {"DEFAULT_TRACKER": "github", "GH_ORG": "acme", "GH_REPO": "widgets"},
       "bitbucket": {"DEFAULT_TRACKER": "jira", "BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "repo", "BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}}


def audited_pr(number):
    """A PR as pr_fetcher returns it after fetching and classifying its comments (the audit needs the bodies anyway)."""
    comments = [{"author": "bob", "body": "looks fine"}, {"author": "coderabbitai[bot]", "body": "[critical] sql injection", "author_kind": "Bot"},
                {"author": "alice", "body": "fixed"}, {"author": "bob", "body": "please rename"}]
    return {"number": number, "state": "merged", "author": "alice", "reviewers": ["carol"], "approvers": ["bob"],
            "total_loc": 55, "file_count": 2, "created_at": "2026-09-25T10:00:00Z", **classify_comments(comments)}


@pytest.fixture(params=["github", "bitbucket"])
def audit_ctx(request, tmp_path, monkeypatch):
    tracker = request.param
    if tracker == "github":
        gh = FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node(7)))
        yield tracker, CFG[tracker], gh, tmp_path
    else:
        with FakeBitbucket(monkeypatch) as bb:
            bb_story(bb, 7)
            yield tracker, CFG[tracker], bb, tmp_path


def test_comments_and_reviewers_come_from_the_audit_data_and_tasks_commits_from_the_batched_fetch(audit_ctx):
    tracker, cfg, fake, tmp_path = audit_ctx
    prs = [audited_pr(7)]
    _attach_review_signals(prs, cfg, tmp_path)
    s = prs[0]["signals"]
    assert (s["comments_human"], s["comments_bot"], s["major_comments_bot"]) == (3, 1, 1)       # from the fetched comments
    assert (s["tasks_human"], s["tasks_bot"], s["tasks_unresolved"], s["commits"]) == (1, 1, 1, 2)   # from the tracker
    assert {r["name"] for r in s["reviewers"]} == {"bob", "carol", "coderabbitai[bot]"}
    if tracker == "github":
        assert "comments(first:100)" not in " ".join(fake.calls[0])      # the audit does not ask for comments twice
    else:
        assert fake.count("/comments") == 0 and fake.count("/tasks") == 1


def test_dashboard_is_the_only_metrics_table_and_keeps_every_row(audit_ctx):
    tracker, cfg, fake, tmp_path = audit_ctx
    prs = [audited_pr(7)]
    _attach_review_signals(prs, cfg, tmp_path)
    written = "# Audit\n\n## Findings\nx\n\n## Metrics Dashboard\n\n| Metric | Value | Benchmark | Signal | Description |\n|---|---|---|---|---|\n| Spec coverage | 40% | ≥80% | 🔴 | d |\n"
    md, rows = extract_and_strip_metrics_tables(written)
    out = md + "\n" + _build_metrics_summary_with_claude(prs, rows)
    assert out.count("### Metrics Dashboard") == 1 and "## Metrics Dashboard\n" not in md
    for name in ("Spec coverage", "PRs without a human comment", "Review tasks (open/total)", "Avg commits per PR", "Major/critical issues flagged"):
        assert name in out
    per_pr = [l for l in out.splitlines() if l.startswith("| ") and "Comments" in l and "Reviewers" in l][0]
    assert all(h in per_pr for h in ("Comments", "Tasks", "Major/Critical", "Commits"))


def test_a_failed_tracker_call_leaves_comment_metrics_and_marks_the_rest_unavailable(tmp_path, monkeypatch):
    FakeGh(tmp_path, monkeypatch).on("graphql", "", rc=1, stderr="boom")
    prs = [audited_pr(7)]
    _attach_review_signals(prs, CFG["github"], tmp_path)
    s = prs[0]["signals"]
    assert s["comments_human"] == 3 and "commits" not in s and "tasks_human" not in s


def test_a_failed_tracker_call_shows_na_for_tasks_and_commits_and_keeps_the_comment_metrics(tmp_path, monkeypatch):
    from scripts.common.pr_metrics import build_pr_table
    FakeGh(tmp_path, monkeypatch).on("graphql", "", rc=1, stderr="boom")
    prs = [audited_pr(7)]
    _attach_review_signals(prs, CFG["github"], tmp_path)
    header = [c.strip() for c in build_pr_table(prs, TABLE_COLUMNS)[0].strip("|").split("|")]
    row = dict(zip(header, [c.strip() for c in build_pr_table(prs, TABLE_COLUMNS)[2].strip("|").split("|")]))
    assert row["Tasks"] == "n/a" and row["Commits"] == "n/a" and row["Comments"] == "3 👤 · 1 🤖"
