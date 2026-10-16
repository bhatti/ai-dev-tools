"""Tests for scripts/common/pr_signals (GitHub + Bitbucket adapters, summarize, cache, retry).

`gh` is a real executable on PATH (tests/fakes/gh.py) and Bitbucket is a real local HTTP server reached through
BITBUCKET_API_BASE (tests/fakes/bitbucket.py): the production code runs unmodified.
"""

from __future__ import annotations

import json

import pytest

from scripts.common import pr_signals as ps
from scripts.common.pr_signals import retry
from tests.fakes.bitbucket import FakeBitbucket
from tests.fakes.gh import FakeGh
from tests.fakes.stories import EXPECTED, bb_story, gh_node, gh_response, user

GH_CFG = {"GH_ORG": "acme", "GH_REPO": "widgets"}
BB_CFG = {"BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "repo", "BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    sleeps: list[float] = []
    monkeypatch.setattr(retry.time, "sleep", sleeps.append)
    return sleeps


def pick(rec):
    return {k: rec[k] for k in EXPECTED}


class TestGitHub:
    def test_metrics_for_a_pr_with_humans_bots_threads_and_a_merge_commit(self, tmp_path, monkeypatch):
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node()))
        rec = ps.collect_signals([{"number": 7}], "github", GH_CFG)[7]
        assert pick(rec) == EXPECTED
        assert rec["error"] == "" and rec["truncated"] == []

    def test_bot_reviewer_who_only_commented_is_a_reviewer(self, tmp_path, monkeypatch):
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node()))
        names = {r["name"]: r for r in ps.collect_signals([{"number": 7}], "github", GH_CFG)[7]["reviewers"]}
        assert names["coderabbitai[bot]"]["is_bot"] and names["coderabbitai[bot]"]["sources"] == ["comment"]
        assert names["carol"]["sources"] == ["assigned"] and "alice" not in names

    def test_many_prs_cost_few_requests(self, tmp_path, monkeypatch):
        nodes = [gh_node(n) for n in range(1, 26)]
        gh = FakeGh(tmp_path, monkeypatch).on("graphql", {"data": {"repository": {f"p{n['number']}": n for n in nodes}}})
        out = ps.collect_signals([{"number": n["number"]} for n in nodes], "github", GH_CFG)
        assert len(out) == 25 and len(gh.calls_matching("graphql")) == 3   # 25 PRs, batches of 10

    def test_only_requested_groups_are_queried(self, tmp_path, monkeypatch):
        gh = FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node()))
        rec = ps.collect_signals([{"number": 7}], "github", GH_CFG, needs=("commits",))[7]
        query = " ".join(gh.calls[0])
        assert "commits(first" in query and "reviewThreads" not in query and "comments(first:100)" not in query
        assert rec["commits"] == 2 and "comments_human" not in rec

    def test_rate_limit_is_retried_with_backoff(self, tmp_path, monkeypatch, _env):
        FakeGh(tmp_path, monkeypatch).on("graphql", "", rc=1, stderr="API rate limit exceeded", once=True).on("graphql", gh_response(gh_node()))
        rec = ps.collect_signals([{"number": 7}], "github", GH_CFG)[7]
        assert rec["error"] == "" and _env == [1.0]

    def test_persistent_failure_is_an_explicit_error_not_zeros(self, tmp_path, monkeypatch, _env):
        FakeGh(tmp_path, monkeypatch).on("graphql", "", rc=1, stderr="API rate limit exceeded")
        rec = ps.collect_signals([{"number": 7}], "github", GH_CFG)[7]
        assert "rate limit" in rec["error"] and "comments_human" not in rec and len(_env) == retry.ATTEMPTS - 1

    def test_pr_missing_from_the_response_is_not_found(self, tmp_path, monkeypatch):
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node(7)))
        assert ps.collect_signals([{"number": 8}], "github", GH_CFG)[8]["error"] == "not found"

    def test_caps_are_reported(self, tmp_path, monkeypatch):
        node = gh_node()
        node["comments"]["totalCount"] = 250
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(node))
        assert ps.collect_signals([{"number": 7}], "github", GH_CFG)[7]["truncated"] == ["comments"]


class TestBitbucket:
    def test_same_story_gives_the_same_metrics(self, monkeypatch):
        with FakeBitbucket(monkeypatch) as bb:
            bb_story(bb)
            rec = ps.collect_signals([{"id": 5}], "bitbucket", BB_CFG)[5]
        assert pick(rec) == EXPECTED   # GitHub <=> Bitbucket parity

    def test_paginated_comments_and_deleted_comments(self, monkeypatch):
        with FakeBitbucket(monkeypatch) as bb:
            bb_story(bb)
            ps.collect_signals([{"id": 5}], "bitbucket", BB_CFG)
            assert bb.count("/comments") == 2   # two pages

    def test_zero_comment_count_skips_the_comments_call(self, monkeypatch):
        with FakeBitbucket(monkeypatch) as bb:
            bb.add("/repositories/ws/repo/pullrequests/9", {"author": {"display_name": "alice"}, "comment_count": 0, "task_count": 0,
                                                            "reviewers": [], "participants": []})
            bb.add_pages("/repositories/ws/repo/pullrequests/9/commits", [{"values": [{"hash": "a", "parents": [{"hash": "x"}]}]}])
            bb.add_pages("/repositories/ws/repo/pullrequests/9/tasks", [{"values": []}])
            rec = ps.collect_signals([{"id": 9}], "bitbucket", BB_CFG)[9]
            assert (bb.count("/comments"), bb.count("/tasks")) == (0, 1)     # comment_count gates comments; tasks are always read
        assert (rec["comments_human"], rec["tasks_human"], rec["commits"]) == (0, 0, 1)

    def test_only_requested_groups_are_fetched(self, monkeypatch):
        with FakeBitbucket(monkeypatch) as bb:
            bb_story(bb)
            ps.collect_signals([{"id": 5}], "bitbucket", BB_CFG, needs=("commits",))
            assert (bb.count("/comments"), bb.count("/tasks"), bb.count("/commits")) == (0, 0, 1)

    def test_429_with_retry_after_is_retried(self, monkeypatch, _env):
        with FakeBitbucket(monkeypatch) as bb:
            bb_story(bb)
            bb.fail("/repositories/ws/repo/pullrequests/5", 429, headers={"Retry-After": "7"})
            rec = ps.collect_signals([{"id": 5}], "bitbucket", BB_CFG)[5]
        assert rec["error"] == "" and _env == [7.0]

    def test_missing_pr_is_not_found(self, monkeypatch):
        with FakeBitbucket(monkeypatch):
            assert ps.collect_signals([{"id": 404}], "bitbucket", BB_CFG)[404]["error"] == "not found"

    def test_server_errors_become_an_explicit_error(self, monkeypatch):
        with FakeBitbucket(monkeypatch) as bb:
            bb_story(bb)
            bb.fail("/repositories/ws/repo/pullrequests/5/tasks", 500, times=10)
            rec = ps.collect_signals([{"id": 5}], "bitbucket", BB_CFG)[5]
        assert "500" in rec["error"]


class TestCacheAndSummarize:
    def test_second_call_is_served_from_the_cache(self, tmp_path, monkeypatch):
        gh = FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node()))
        ps.collect_signals([{"number": 7}], "github", GH_CFG)
        again = ps.collect_signals([{"number": 7}], "github", GH_CFG)
        assert len(gh.calls) == 1 and pick(again[7]) == EXPECTED

    def test_asking_for_more_groups_fetches_again_and_keeps_the_old_ones(self, tmp_path, monkeypatch):
        gh = FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node()))
        ps.collect_signals([{"number": 7}], "github", GH_CFG, needs=("commits",))
        rec = ps.collect_signals([{"number": 7}], "github", GH_CFG, needs=("commits", "tasks"))[7]
        assert len(gh.calls) == 2 and rec["commits"] == 2 and rec["tasks_human"] == 1

    def test_failures_are_not_cached(self, tmp_path, monkeypatch):
        gh = FakeGh(tmp_path, monkeypatch).on("graphql", "", rc=1, stderr="boom")
        ps.collect_signals([{"number": 7}], "github", GH_CFG)
        ps.collect_signals([{"number": 7}], "github", GH_CFG)
        assert len(gh.calls) == 2

    def test_cache_for_another_tracker_is_ignored(self, tmp_path, monkeypatch):
        gh = FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node()))
        (tmp_path / "ws").mkdir()
        (tmp_path / "ws" / "pr_signals.json").write_text(json.dumps({"tracker": "bitbucket", "prs": {"7": {"groups": list(ps.GROUPS), "commits": 99}}}))
        assert ps.collect_signals([{"number": 7}], "github", GH_CFG)[7]["commits"] == 2 and len(gh.calls) == 1

    def test_summarize_errors_pass_through(self):
        assert ps.summarize({"error": "x"}, {"comments"}) == {"error": "x", "groups": []}

    def test_prs_without_a_number_are_skipped(self, tmp_path, monkeypatch):
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node()))
        assert ps.collect_signals([{"title": "x"}, {"number": 7}], "github", GH_CFG).keys() == {7}


class TestCli:
    def test_prints_records_as_json_and_exits_0(self, tmp_path, monkeypatch):
        import subprocess, sys
        from pathlib import Path
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node()))
        env = {"PATH": __import__("os").environ["PATH"], "WORKSPACE_DIR": str(tmp_path / "ws"), "GH_ORG": "acme", "GH_REPO": "widgets",
               "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
        r = subprocess.run([sys.executable, "-m", "scripts.common.pr_signals", "--tracker", "github", "--needs", "commits,tasks", "7"],
                           capture_output=True, text=True, env=env, cwd=Path(__file__).resolve().parent.parent)
        assert r.returncode == 0 and json.loads(r.stdout)["7"]["commits"] == 2

    def test_exit_1_when_a_pr_could_not_be_read(self, tmp_path, monkeypatch):
        import subprocess, sys
        from pathlib import Path
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node()))
        env = {"PATH": __import__("os").environ["PATH"], "WORKSPACE_DIR": str(tmp_path / "ws"), "GH_ORG": "acme", "GH_REPO": "widgets",
               "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
        r = subprocess.run([sys.executable, "-m", "scripts.common.pr_signals", "--tracker", "github", "404"], capture_output=True, text=True,
                           env=env, cwd=Path(__file__).resolve().parent.parent)
        assert r.returncode == 1 and json.loads(r.stdout)["404"]["error"] == "not found"

    def test_unknown_group_is_a_usage_error(self):
        from scripts.common.pr_signals.__main__ import main
        assert main(["--tracker", "github", "--needs", "bogus", "1"]) == 2


def test_graphql_errors_are_reported_not_mistaken_for_a_missing_pr(tmp_path, monkeypatch):
    FakeGh(tmp_path, monkeypatch).on("graphql", {"data": {"repository": {"p7": None}},
                                                 "errors": [{"message": "You must provide a `first` or `last` value to properly paginate the `parents` connection."}]})
    rec = ps.collect_signals([{"number": 7}], "github", GH_CFG)[7]
    assert "paginate" in rec["error"] and rec["error"] != "not found"


def test_every_graphql_connection_has_a_page_size():
    """GitHub rejects a connection without first/last, which makes the whole PR come back null."""
    from scripts.common.pr_signals import github
    query = github.build_query([1], {"comments", "reviewers", "tasks", "commits"})
    for connection in ("comments", "reviews", "reviewRequests", "reviewThreads", "commits", "parents"):
        assert f"{connection}(first:" in query, connection


def test_the_query_needs_no_org_scope():
    """`Team { name }` needs read:org, which job tokens usually lack; a query that asks for it fails for every PR."""
    from scripts.common.pr_signals import github
    assert "Team" not in github.build_query([1], {"reviewers"})


class TestHardening:
    def test_bitbucket_requests_ask_for_the_fields_the_metrics_need(self, monkeypatch):
        from urllib.parse import unquote
        with FakeBitbucket(monkeypatch) as bb:
            bb_story(bb)
            ps.collect_signals([{"id": 5}], "bitbucket", BB_CFG)
            pr_q = unquote(bb.query("/pullrequests/5"))
            for field in ("participants.role", "participants.approved", "comment_count", "author.account_type", "reviewers.account_type"):
                assert field in pr_q, field
            assert "values.user.account_type" in unquote(bb.query("/comments")) and "values.creator.account_type" in unquote(bb.query("/tasks"))
            assert "values.parents.hash" in unquote(bb.query("/commits"))

    def test_non_json_response_is_an_error_for_that_pr_only(self, monkeypatch):
        with FakeBitbucket(monkeypatch) as bb:
            bb_story(bb, 5)
            bb.add_raw("/repositories/ws/repo/pullrequests/6", "<html>gateway error</html>")
            out = ps.collect_signals([{"id": 5}, {"id": 6}], "bitbucket", BB_CFG)
        assert pick(out[5]) == EXPECTED and "non-JSON" in out[6]["error"]

    def test_missing_credentials_are_an_error_not_a_crash(self, monkeypatch):
        with FakeBitbucket(monkeypatch) as bb:
            bb_story(bb)
            rec = ps.collect_signals([{"id": 5}], "bitbucket", {k: v for k, v in BB_CFG.items() if k != "BITBUCKET_USERNAME"})[5]
        assert "BITBUCKET_USERNAME" in rec["error"]

    @pytest.mark.parametrize("missing", ["comments", "tasks", "commits"])
    def test_a_404_on_a_sub_list_is_an_error_not_zero(self, monkeypatch, missing):
        with FakeBitbucket(monkeypatch) as bb:
            bb_story(bb)
            del bb.routes[f"/2.0/repositories/ws/repo/pullrequests/5/{missing}"]
            rec = ps.collect_signals([{"id": 5}], "bitbucket", BB_CFG)[5]
        assert "404" in rec["error"] and "comments_human" not in rec

    def test_a_later_plain_review_comment_does_not_withdraw_an_approval(self, tmp_path, monkeypatch):
        node = gh_node()
        node["reviews"]["nodes"].append({"state": "COMMENTED", "body": "one more thought", "author": user("bob")})
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(node))
        assert ps.collect_signals([{"number": 7}], "github", GH_CFG)[7]["approvers"] == ["bob"]

    @pytest.mark.parametrize("states,approved", [(["APPROVED", "CHANGES_REQUESTED"], False), (["APPROVED", "DISMISSED"], False),
                                                 (["CHANGES_REQUESTED", "APPROVED"], True), (["PENDING"], False)])
    def test_review_decisions(self, tmp_path, monkeypatch, states, approved):
        node = gh_node()
        node["reviews"]["nodes"] = [{"state": st, "body": "", "author": user("bob")} for st in states]
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(node))
        assert (ps.collect_signals([{"number": 7}], "github", GH_CFG)[7]["approvers"] == ["bob"]) is approved

    def test_cut_review_threads_mark_comments_and_tasks_truncated(self, tmp_path, monkeypatch):
        node = gh_node()
        node["reviewThreads"]["totalCount"] = 120
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(node))
        assert set(ps.collect_signals([{"number": 7}], "github", GH_CFG)[7]["truncated"]) == {"comments", "tasks"}

    def test_inner_thread_comments_cut_mark_comments_truncated(self, tmp_path, monkeypatch):
        node = gh_node()
        node["reviewThreads"]["nodes"][0]["comments"]["totalCount"] = 60
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(node))
        assert ps.collect_signals([{"number": 7}], "github", GH_CFG)[7]["truncated"] == ["comments"]

    def test_cache_for_another_repo_is_not_reused(self, tmp_path, monkeypatch):
        gh = FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node()))
        ps.collect_signals([{"number": 7}], "github", GH_CFG)
        ps.collect_signals([{"number": 7}], "github", {"GH_ORG": "acme", "GH_REPO": "other"})
        assert len(gh.calls) == 2

    def test_a_failed_refresh_keeps_the_good_cached_record(self, tmp_path, monkeypatch):
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node()), once=True).on("graphql", "", rc=1, stderr="boom")
        ps.collect_signals([{"number": 7}], "github", GH_CFG, needs=("commits",))
        again = ps.collect_signals([{"number": 7}], "github", GH_CFG, needs=("commits", "tasks"))[7]
        assert not again.get("error") and again["commits"] == 2

    @pytest.mark.parametrize("content", ["[]", "null", "{\"prs\": []}", "not json"])
    def test_unreadable_cache_files_are_ignored(self, tmp_path, monkeypatch, content):
        (tmp_path / "ws").mkdir()
        (tmp_path / "ws" / "pr_signals.json").write_text(content)
        FakeGh(tmp_path, monkeypatch).on("graphql", gh_response(gh_node()))
        assert ps.read_cached(7) is None and ps.collect_signals([{"number": 7}], "github", GH_CFG)[7]["commits"] == 2

    def test_tracker_name(self):
        assert [ps.tracker_name(t) for t in ("jira", "jira/bitbucket", "Bitbucket", "github", "", None)] == ["bitbucket"] * 3 + ["github"] * 3
