"""Tests for scripts/tickets/fetch.py and the two tracker adapters, against the fake Jira server and the fake gh."""

from __future__ import annotations

import json
import subprocess

import pytest

from scripts.common import adf, http_retry, jira_api
from scripts.tickets import fetch, github
from scripts.tickets.github import GitHubTickets
from tests.fakes import ticket_story
from tests.fakes.gh_issues import FakeGhIssues
from tests.fakes.jira import FakeJira


@pytest.fixture()
def jira(monkeypatch):
    jira_api._field_id_cache.clear()
    with FakeJira(monkeypatch) as fake:
        yield fake


@pytest.fixture()
def gh(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_ORG", "acme")
    monkeypatch.setenv("GH_REPO", "widgets")
    return FakeGhIssues(tmp_path, monkeypatch)


def run(tmp_path, *argv):
    out = tmp_path / "tickets.json"
    code = fetch.main([*argv, "--out", str(out)])
    return code, (json.loads(out.read_text()) if out.exists() else None)


def facts(t):
    return {"title": t["title"], "points": t["story_points"]["value"], "blocked": t["blocked"], "in_flight": t["in_flight"],
            "has_impl_plan": t["has_impl_plan"], "gwt": t["signals"]["given_when_then_lines"],
            "behavior": bool(t["signals"]["given_when_then_lines"] or t["signals"]["ac_header"] or t["signals"]["spec_headers"])}


class TestParity:
    def test_jira_and_github_tell_the_same_story(self, tmp_path, jira, gh):
        ticket_story.load_jira(jira)
        ticket_story.load_github(gh)
        code_j, jira_out = run(tmp_path, "--sprint", "Sprint 1", "--project", "PROJ", "--tracker", "jira")
        code_g, gh_out = run(tmp_path, "--milestone", "Sprint 1", "--tracker", "github")
        assert code_j == code_g == 0
        assert [facts(t) for t in jira_out["tickets"]] == [facts(t) for t in gh_out["tickets"]] == [ticket_story.EXPECTED[n] for n in (1, 2, 3, 4)]
        assert [t["id"] for t in jira_out["tickets"]] == ["PROJ-1", "PROJ-2", "PROJ-3", "PROJ-4"]
        assert [t["id"] for t in gh_out["tickets"]] == ["#1", "#2", "#3", "#4"]

    def test_a_github_sprint_flag_means_the_milestone(self, tmp_path, gh):
        ticket_story.load_github(gh)
        code, out = run(tmp_path, "--sprint", "Sprint 1", "--tracker", "github")
        assert code == 0 and out["count"] == 4


class TestJira:
    def test_pagination_and_the_default_done_filter(self, tmp_path, jira):
        for i in range(1, 6):
            jira.add_issue(f"PROJ-{i}", f"t{i}", description="x")
        jira.page_size = 2
        code, out = run(tmp_path, "--sprint", "S", "--project", "PROJ", "--tracker", "jira")
        assert code == 0 and out["count"] == 5
        jql = next(b["jql"] for m, p, b in jira.requests if p.endswith("/search/jql"))
        assert 'sprint = "S"' in jql and 'project = "PROJ"' in jql and "statusCategory" not in jql   # a sprint is read whole: finished work is the velocity
        run(tmp_path, "--label", "x", "--project", "PROJ", "--tracker", "jira")
        assert "statusCategory != Done" in [b["jql"] for m, p, b in jira.requests if p.endswith("/search/jql")][-1]   # other scopes still hide finished work
        run(tmp_path, "--label", "x", "--project", "PROJ", "--tracker", "jira", "--include-done")
        assert "statusCategory" not in [b["jql"] for m, p, b in jira.requests if p.endswith("/search/jql")][-1]

    def test_story_points_come_from_the_site_field_whatever_its_name(self, tmp_path, jira):
        jira.add_field("customfield_77", "Story Points (Fibonacci Sequence)")
        jira.add_issue("PROJ-1", "t", description="x", points=None)
        jira.issues["PROJ-1"]["fields"]["customfield_77"] = 8
        _, out = run(tmp_path, "PROJ-1", "--tracker", "jira")
        assert out["tickets"][0]["story_points"] == {"value": 8.0, "source": "Story Points (Fibonacci Sequence)", "known": True}

    def test_unset_points_are_known_and_unreadable_ones_are_not(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "t", description="x")
        _, out = run(tmp_path, "PROJ-1", "--tracker", "jira")
        assert out["tickets"][0]["story_points"]["known"] is True and out["tickets"][0]["story_points"]["value"] is None
        jira_api._field_id_cache.clear()
        jira.fail("/rest/api/3/field", 403)
        _, out = run(tmp_path, "PROJ-1", "--tracker", "jira")
        assert out["tickets"][0]["story_points"]["known"] is False

    def test_a_missing_ticket_is_kept_as_unreadable_not_dropped(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "t", description="x")
        code, out = run(tmp_path, "PROJ-1", "PROJ-404", "--tracker", "jira")
        assert code == 0 and out["tickets"][1]["fetch_error"] and out["errors"][0]["id"] == "PROJ-404"

    def test_access_denied_is_flagged(self, tmp_path, jira):
        jira.fail("/rest/api/3/issue/PROJ-1", 403)
        _, out = run(tmp_path, "PROJ-1", "--tracker", "jira")
        assert out["tickets"][0]["access_denied"] is True

    def test_sub_tasks_carry_their_parent_context(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "parent story", description="## Why\nbecause of reasons")
        jira.add_issue("PROJ-2", "child", issue_type="Sub-task", parent="PROJ-1")
        _, out = run(tmp_path, "PROJ-2", "--tracker", "jira")
        t = out["tickets"][0]
        assert t["is_subtask"] and t["parent"]["id"] == "PROJ-1" and "because of reasons" in t["parent"]["description"]

    def test_blocking_links_to_open_tickets_block(self, tmp_path, jira):
        link = {"type": {"inward": "is blocked by"}, "inwardIssue": {"key": "PROJ-9", "fields": {"status": {"statusCategory": {"key": "new"}}}}}
        done = {"type": {"inward": "is blocked by"}, "inwardIssue": {"key": "PROJ-8", "fields": {"status": {"statusCategory": {"key": "done"}}}}}
        jira.add_issue("PROJ-1", "t", description="x", links=[link, done])
        _, out = run(tmp_path, "PROJ-1", "--tracker", "jira")
        assert out["tickets"][0]["blocked"] and out["tickets"][0]["blocked_by"] == ["PROJ-9"]

    def test_browse_urls_are_accepted_and_missing_credentials_are_blocked(self, tmp_path, jira, monkeypatch, capsys):
        jira.add_issue("PROJ-1", "t", description="x")
        _, out = run(tmp_path, f"{jira.base}/browse/PROJ-1")
        assert out["tracker"] == "jira" and out["tickets"][0]["id"] == "PROJ-1"
        monkeypatch.delenv("JIRA_API_TOKEN")
        assert fetch.main(["PROJ-1", "--tracker", "jira"]) == 3
        assert "JIRA_API_TOKEN" in capsys.readouterr().out

    def test_a_failed_search_is_blocked_not_empty(self, tmp_path, jira, capsys):
        jira.fail("/search/jql", 400)
        code, _ = run(tmp_path, "--jql", "bad", "--tracker", "jira")
        assert code == 3 and "Jira search failed" in capsys.readouterr().out

    def test_no_matching_ticket_is_blocked(self, tmp_path, jira, capsys):
        jira.add_search("sprint", [])
        code, _ = run(tmp_path, "--sprint", "Nope", "--tracker", "jira", "--project", "PROJ")
        assert code == 3 and "no tickets matched" in capsys.readouterr().out


class TestGitHub:
    def test_refs_in_every_form(self, tmp_path, gh):
        gh.add_issue(7, "t", body="x")
        _, out = run(tmp_path, "7", "#7", "acme/widgets#7", "https://github.com/acme/widgets/issues/7", "--tracker", "github")
        assert [t["id"] for t in out["tickets"]] == ["#7"] * 4

    def test_a_missing_issue_is_unreadable(self, tmp_path, gh):
        _, out = run(tmp_path, "999", "--tracker", "github")
        assert out["tickets"][0]["fetch_error"] and out["tickets"][0]["access_denied"] is False

    def test_closed_issues_need_include_done(self, tmp_path, gh):
        gh.add_issue(1, "open", body="x", milestone="M").add_issue(2, "closed", body="x", milestone="M", state="CLOSED")
        out = run(tmp_path, "--milestone", "M", "--tracker", "github")[1]
        assert out["count"] == 2 and out["evaluated"] == 1 and [t["id"] for t in out["tickets"]] == ["#1"]   # finished work is read for the totals, not graded
        assert out["load"]["lanes"]["done"]["count"] == 1
        assert run(tmp_path, "--milestone", "M", "--tracker", "github", "--include-done")[1]["evaluated"] == 2

    def test_a_repository_is_required_for_a_search(self, tmp_path, gh, monkeypatch, capsys):
        monkeypatch.delenv("GH_ORG")
        code, _ = run(tmp_path, "--label", "x", "--tracker", "github")
        assert code == 3 and "owner/name" in capsys.readouterr().out

    def test_project_field_points_win_and_an_unreadable_project_is_unknown(self, tmp_path, gh, monkeypatch):
        monkeypatch.setenv("GH_PROJECT_NUMBER", "4")
        gh.add_issue(1, "t", body="x", labels=["sp:1"])
        gh.set("graphql", {"data": {"repository": {"issue": {"projectItems": {"nodes": [
            {"project": {"number": 4}, "fieldValues": {"nodes": [{"number": 13, "field": {"name": "Story points"}}]}}]}}}}})
        assert run(tmp_path, "1", "--tracker", "github")[1]["tickets"][0]["story_points"]["value"] == 13.0
        gh.set("graphql_fail", "missing scope read:project")
        sp = run(tmp_path, "1", "--tracker", "github")[1]["tickets"][0]["story_points"]
        assert sp["value"] == 1.0 and sp["source"] == "label"  # the label still answers
        gh.add_issue(2, "t", body="x")
        assert run(tmp_path, "2", "--tracker", "github")[1]["tickets"][0]["story_points"]["known"] is False


class TestArguments:
    @pytest.mark.parametrize("argv", [[], ["PROJ-1", "--sprint", "S"], ["--limit", "0", "--sprint", "S"], ["--milestone", "M", "--sprint", "S"],
                                      ["--jql", "x", "--sprint", "S"], ["--jql", "x", "--label", "l"]])
    def test_bad_scopes_are_usage_errors(self, argv, capsys):
        assert fetch.main(argv) == 2

    def test_jql_is_jira_only(self, gh, capsys):
        assert fetch.main(["--jql", "x", "--tracker", "github"]) == 2

    def test_default_tracker_decides_when_nothing_else_does(self, tmp_path, jira, monkeypatch):
        jira.add_issue("PROJ-1", "t", description="x")
        monkeypatch.setenv("DEFAULT_TRACKER", "jira")
        code, out = run(tmp_path, "--label", "x")
        assert code == 0 and out["tracker"] == "jira" and out["tickets"][0]["id"] == "PROJ-1"


class TestNothingTruncated:
    """The whole description and every comment reach the grader: a clipped acceptance criterion is a wrong verdict."""

    LONG = "## Acceptance Criteria\n" + "\n".join(f"- Given case {i}, when x, then y{i}. Test: make t{i}" for i in range(3000))

    def test_jira_description_and_comments_arrive_whole(self, tmp_path, jira):
        comments = [f"clarification {i}: " + "z" * 2000 for i in range(8)]
        jira.add_issue("PROJ-1", "t", description=self.LONG, comments=comments)
        _, out = run(tmp_path, "PROJ-1", "--tracker", "jira")
        t = out["tickets"][0]
        assert "case 2999," in t["description"] and t["description_chars"] == len(t["description"]) > 100_000
        assert len(t["comments"]) == 8 and all(len(c) > 2000 for c in t["comments"]) and t["comments_total"] == 8

    def test_github_body_and_comments_arrive_whole(self, tmp_path, gh):
        gh.add_issue(1, "t", body=self.LONG, comments=["q" * 5000, "last"])
        _, out = run(tmp_path, "1", "--tracker", "github")
        t = out["tickets"][0]
        assert "case 2999," in t["description"] and t["comments"][0] == "q" * 5000 and t["comments_total"] == 2

    def test_a_parent_description_is_whole(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "parent", description="intro\n" + "p" * 50_000 + "\nfinal parent line")
        jira.add_issue("PROJ-2", "child", issue_type="Sub-task", parent="PROJ-1")
        _, out = run(tmp_path, "PROJ-2", "--tracker", "jira")
        assert out["tickets"][0]["parent"]["description"].endswith("final parent line")

    def test_every_jira_comment_arrives_even_when_the_payload_embeds_only_a_page(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "t", description="x", comments=[f"c{i}" for i in range(60)])
        jira.embedded_comment_cap, jira.comment_page_size = 20, 25
        t = run(tmp_path, "PROJ-1", "--tracker", "jira")[1]["tickets"][0]
        assert t["comments"] == [f"c{i}" for i in range(60)] and t["comments_total"] == 60

    def test_comments_that_cannot_be_read_completely_make_the_ticket_unreadable_not_graded(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "t", description="x", comments=[f"c{i}" for i in range(60)])
        jira.embedded_comment_cap = 20
        jira.fail("/comment", 500, times=20, headers={"Retry-After": "0"}, method="GET")
        t = run(tmp_path, "PROJ-1", "--tracker", "jira")[1]["tickets"][0]
        assert "comments of PROJ-1 could not be read completely" in t["fetch_error"]

    def test_github_issues_at_the_list_comment_cap_are_read_again_in_full(self, tmp_path, gh):
        gh.add_issue(1, "busy", body="x", milestone="M", comments=[f"c{i}" for i in range(130)])
        gh.add_issue(2, "quiet", body="x", milestone="M", comments=["only"])
        out = run(tmp_path, "--milestone", "M", "--tracker", "github")[1]["tickets"]
        assert out[0]["comments_total"] == 130 and out[0]["comments"][-1] == "c129" and out[1]["comments"] == ["only"]
        assert len(gh.calls_matching("issue view 1")) == 1 and not gh.calls_matching("issue view 2")

    def test_the_environment_field_reaches_the_grader(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "t", description="x")
        jira.issues["PROJ-1"]["fields"]["environment"] = {"type": "doc", "version": 1, "content": adf.from_markdown("Runs on arm64 only")}
        assert run(tmp_path, "PROJ-1", "--tracker", "jira")[1]["tickets"][0]["environment"] == "Runs on arm64 only"


class TestGitHubReliability:
    def test_rate_limits_are_retried(self, tmp_path, gh, monkeypatch):
        monkeypatch.setattr(http_retry, "BASE_DELAY", 0.0)
        gh.add_issue(1, "t", body="x")
        gh.set("transient", 2)
        assert run(tmp_path, "1", "--tracker", "github")[1]["tickets"][0]["fetch_error"] == ""

    def test_output_that_is_not_json_is_an_unreadable_ticket_not_a_crash(self, tmp_path, gh):
        gh.add_issue(1, "t", body="x")
        gh.set("garbage", True)
        t = run(tmp_path, "1", "--tracker", "github")[1]["tickets"][0]
        assert "not JSON" in t["fetch_error"]

    def test_a_project_with_more_items_than_one_page_is_unknown_not_unset(self, tmp_path, gh, monkeypatch):
        monkeypatch.setenv("GH_PROJECT_NUMBER", "4")
        gh.add_issue(1, "t", body="x")
        gh.set("graphql", {"data": {"repository": {"issue": {"projectItems": {"pageInfo": {"hasNextPage": True}, "nodes": []}}}}})
        assert run(tmp_path, "1", "--tracker", "github")[1]["tickets"][0]["story_points"]["known"] is False


class TestScaleAndCost:
    def test_a_result_cut_at_the_limit_says_so(self, tmp_path, jira, capsys):
        for i in range(1, 6):
            jira.add_issue(f"PROJ-{i}", f"t{i}", description="x")
        code, out = run(tmp_path, "--sprint", "S", "--project", "PROJ", "--tracker", "jira", "--limit", "3")
        assert code == 0 and out["count"] == 3 and out["truncated"] is True
        assert "--limit" in capsys.readouterr().err
        assert run(tmp_path, "--sprint", "S", "--project", "PROJ", "--tracker", "jira")[1]["truncated"] is False

    def test_sub_tasks_of_one_parent_fetch_the_parent_once(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "parent", description="the why")
        for i in (2, 3, 4):
            jira.add_issue(f"PROJ-{i}", f"child {i}", issue_type="Sub-task", parent="PROJ-1")
        run(tmp_path, "PROJ-2", "PROJ-3", "PROJ-4", "--tracker", "jira")
        parent_reads = [r for r in jira.requests if r[0] == "GET" and r[1].endswith("/issue/PROJ-1")]
        assert len(parent_reads) == 1


class TestOneBadTicketNeverFailsTheBatch:
    def test_a_sprint_search_keeps_going_when_one_ticket_cannot_be_read_completely(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "fine", description="x", comments=["a"])
        jira.add_issue("PROJ-2", "many comments", description="x", comments=[f"c{i}" for i in range(30)])
        jira.add_issue("PROJ-3", "fine too", description="x")
        jira.embedded_comment_cap = 10
        jira.fail("/PROJ-2/comment", 500, times=20, headers={"Retry-After": "0"}, method="GET")
        code, out = run(tmp_path, "--sprint", "Sprint", "--project", "PROJ", "--tracker", "jira")
        by_id = {t["id"]: t for t in out["tickets"]}
        assert code == 0 and "could not be read completely" in by_id["PROJ-2"]["fetch_error"] and by_id["PROJ-1"]["fetch_error"] == "" and by_id["PROJ-3"]["fetch_error"] == ""

    def test_an_unexpected_ticket_structure_is_unreadable_not_a_crash(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "weird", description=None)
        jira.issues["PROJ-1"]["fields"]["issuelinks"] = "not a list"
        jira.add_issue("PROJ-2", "fine", description="x")
        out = run(tmp_path, "--sprint", "Sprint", "--project", "PROJ", "--tracker", "jira")[1]["tickets"]
        assert "unexpected ticket structure" in out[0]["fetch_error"] and out[1]["fetch_error"] == ""

    def test_a_malformed_github_ref_is_one_unreadable_entry(self, tmp_path, gh):
        gh.add_issue(1, "ok", body="x")
        out = run(tmp_path, "1", "not-a-ticket", "--tracker", "github")[1]["tickets"]
        assert out[0]["fetch_error"] == "" and out[1]["fetch_error"] and out[1]["id"] == "not-a-ticket"

    def test_a_sub_task_whose_parent_cannot_be_read_says_so_and_retries_later(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "parent", description="the why")
        jira.add_issue("PROJ-2", "child", issue_type="Sub-task", parent="PROJ-1")
        jira.add_issue("PROJ-3", "child 2", issue_type="Sub-task", parent="PROJ-1")
        jira.fail("/issue/PROJ-1", 403, times=1, method="GET")
        out = run(tmp_path, "PROJ-2", "PROJ-3", "--tracker", "jira")[1]["tickets"]
        assert "could not be read" in out[0]["parent"]["error"] and out[0]["parent"]["description"] == ""
        assert out[1]["parent"]["error"] == "" and out[1]["parent"]["description"] == "the why"   # the failure was not cached


class TestGitHubCallPolicy:
    def test_in_progress_labels_match_whole_words_only(self, tmp_path, gh):
        gh.add_issue(1, "a", body="x", labels=["in progress"]).add_issue(2, "b", body="x", labels=["swipe-ui", "undoing"]).add_issue(3, "c", body="x", labels=["WIP"])
        by_id = {t["id"]: t for t in run(tmp_path, "1", "2", "3", "--tracker", "github")[1]["tickets"]}
        assert [by_id[i]["in_flight"] for i in ("#1", "#2", "#3")] == [True, False, True]

    def test_a_hung_gh_is_timed_out_not_waited_for_forever(self, tmp_path, gh, monkeypatch):
        monkeypatch.setattr(github, "run_cmd", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("gh", 1)))
        monkeypatch.setattr(http_retry, "BASE_DELAY", 0.0)
        t = run(tmp_path, "1", "--repo", "acme/widgets", "--tracker", "github")[1]["tickets"][0]
        assert "timed out" in t["fetch_error"]

    def test_a_comment_is_never_retried_after_a_server_error_but_is_after_a_rate_limit(self, tmp_path, gh, monkeypatch):
        monkeypatch.setattr(http_retry, "BASE_DELAY", 0.0)
        gh.add_issue(1, "t", body="x")
        client = GitHubTickets("acme/widgets")
        gh.set("transient", 1).set("transient_message", "HTTP 502 Bad Gateway")
        with pytest.raises(Exception, match="502"):
            client.comment("1", "note")
        assert gh.comments(1) == []
        gh.set("transient", 1).set("transient_message", "API rate limit exceeded")
        client.comment("1", "note")
        assert gh.comments(1) == ["note"]


class TestReadableTextFiles:
    """The model reads tickets with a tool that cuts very long lines, so every ticket also gets a plain-text file with real line breaks."""

    def _texts(self, tmp_path):
        return {p.name: p.read_text() for p in (tmp_path / "tickets").glob("*.txt")}

    def test_every_ticket_has_a_file_with_its_complete_text_and_no_overlong_lines(self, tmp_path, jira):
        long_paragraph = "word " * 4000                       # one 20k-character line, as pasted specs often are
        jira.add_issue("PROJ-1", "Long one", description=f"## Why\n{long_paragraph}\n## Acceptance Criteria\n" + "\n".join(f"- criterion {i}" for i in range(500)),
                       comments=["first note", "second " + "x " * 3000], points=3)
        run(tmp_path, "PROJ-1", "--tracker", "jira")
        text = self._texts(tmp_path)["PROJ-1.txt"]
        assert all(len(line) <= 1500 for line in text.splitlines())
        flat = " ".join(text.split())
        assert " ".join(long_paragraph.split()) in flat and "criterion 0" in flat and "criterion 499" in flat
        assert "first note" in flat and flat.count("x ") >= 3000 and "Comment 2 of 2" in text

    def test_the_file_carries_what_a_grader_needs_next_to_the_text(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "Parent", description="the parent why")
        jira.add_issue("PROJ-2", "Child", description="child text", issue_type="Sub-task", parent="PROJ-1", labels=["blocked"], points=2)
        jira.issues["PROJ-2"]["fields"]["environment"] = {"type": "doc", "version": 1, "content": adf.from_markdown("runs on arm64")}
        run(tmp_path, "PROJ-2", "--tracker", "jira")
        text = self._texts(tmp_path)["PROJ-2.txt"]
        for needle in ("PROJ-2", "Child", "Sub-task", "blocked", "2", "child text", "runs on arm64", "Parent ticket PROJ-1", "the parent why"):
            assert needle in text

    def test_github_ids_become_safe_file_names_and_comments_are_all_there(self, tmp_path, gh):
        gh.add_issue(42, "t", body="body text", comments=[f"c{i}" for i in range(130)], milestone="M")
        run(tmp_path, "42", "--tracker", "github")
        text = self._texts(tmp_path)["gh-42.txt"]
        assert "body text" in text and "Comment 130 of 130" in text and "c129" in text

    def test_an_unreadable_ticket_gets_a_file_that_says_so(self, tmp_path, jira):
        run(tmp_path, "PROJ-404", "--tracker", "jira")
        assert "could not be read" in self._texts(tmp_path)["PROJ-404.txt"]

    def test_the_json_names_the_file(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "t", description="x")
        t = run(tmp_path, "PROJ-1", "--tracker", "jira")[1]["tickets"][0]
        assert t["text_file"].endswith("tickets/PROJ-1.txt") and (tmp_path / "tickets" / "PROJ-1.txt").exists()


S29 = {"id": 29, "name": "Sprint 29", "state": "closed", "boardId": 7}
S30 = {"id": 30, "name": "Sprint 30", "state": "future", "boardId": 7}
S28 = {"id": 28, "name": "Sprint 28", "state": "closed", "boardId": 7}


class TestJiraSprintLoad:
    def _sprint(self, jira):
        jira.add_issue("PROJ-1", "not started", description="x", points=3, status="To Do", category="new", sprints=[S29, S30])
        jira.add_issue("PROJ-2", "coding", description="x", points=5, status="In Progress", category="indeterminate", sprints=[S28, S29, S30])
        jira.add_issue("PROJ-3", "reviewing", description="x", points=8, status="Code Complete", category="indeterminate", sprints=[S30])
        jira.add_issue("PROJ-4", "shipped", description="x", points=2, status="Done", category="done", sprints=[S29, S30])
        jira.add_issue("PROJ-5", "no points", description="x", points=None, status="QA", category="indeterminate", sprints=[S30])
        jira.add_issue("PROJ-6", "waiting", description="x", points=1, status="Blocked", category="indeterminate", sprints=[S30])
        jira.add_issue("PROJ-6A", "a sub-task", description="x", points=99, issue_type="Sub-task", parent="PROJ-6", sprints=[S30])
        jira.add_search("Sprint 30", ["PROJ-1", "PROJ-2", "PROJ-3", "PROJ-4", "PROJ-5", "PROJ-6", "PROJ-6A"])

    def test_points_per_lane_come_from_every_ticket_in_the_sprint_but_only_unfinished_ones_are_graded(self, tmp_path, jira):
        self._sprint(jira)
        code, out = run(tmp_path, "--sprint", "Sprint 30", "--project", "PROJ", "--tracker", "jira")
        lanes = out["load"]["lanes"]
        assert code == 0 and out["count"] == 7 and out["evaluated"] == 6 and "PROJ-4" not in [t["id"] for t in out["tickets"]]
        assert (lanes["pending"]["points"], lanes["pending"]["count"]) == (4.0, 2)        # To Do (3) + Blocked (1)
        assert (lanes["in_progress"]["points"], lanes["review"]["points"], lanes["done"]["points"]) == (5.0, 8.0, 2.0)
        assert lanes["review"]["unpointed"] == ["PROJ-5"] and out["load"]["total"]["points"] == 19.0   # the sub-task's 99 is not counted
        assert {t["id"]: t["lane"] for t in out["tickets"]}["PROJ-3"] == "review"

    def test_carry_over_comes_from_the_sprint_history(self, tmp_path, jira):
        self._sprint(jira)
        out = run(tmp_path, "--sprint", "Sprint 30", "--project", "PROJ", "--tracker", "jira")[1]
        c = out["load"]["carry_over"]
        assert c["count"] == 3 and c["chronic"] == ["PROJ-2"] and c["by_lane"]["pending"] == ["PROJ-1"] and c["by_lane"]["done"] == ["PROJ-4"]
        by_id = {t["id"]: t for t in out["tickets"]}
        assert by_id["PROJ-2"]["carry_over"] == {"times": 2, "from_sprints": ["Sprint 28", "Sprint 29"]} and by_id["PROJ-3"]["carry_over"]["times"] == 0
        assert out["load"]["target_sprint"]["name"] == "Sprint 30"

    def test_unfinished_work_of_the_active_sprint_that_is_missing_from_a_future_sprint_is_reported(self, tmp_path, jira):
        self._sprint(jira)
        jira.add_board_sprint(7, {"id": 29, "name": "Sprint 29", "state": "active"}).add_board_sprint(7, {"id": 99, "name": "Other Team Sprint 5", "state": "active"})
        jira.add_issue("PROJ-7", "left behind", description="x", points=3, status="In Progress", category="indeterminate", sprints=[S29])
        jira.add_search("sprint = 29", ["PROJ-1", "PROJ-7"])      # PROJ-1 is already in Sprint 30, PROJ-7 is not
        roll = run(tmp_path, "--sprint", "Sprint 30", "--project", "PROJ", "--tracker", "jira")[1]["load"]["pending_rollover"]
        assert roll["from_sprint"] == "Sprint 29"
        assert {t["id"]: t["already_in_target"] for t in roll["tickets"]} == {"PROJ-1": True, "PROJ-7": False}
        assert any('"Sprint 29"' not in str(b) and "sprint = 29" in str(b) for m, p, b in jira.requests if p.endswith("/search/jql"))

    def test_an_active_target_sprint_has_no_rollover(self, tmp_path, jira):
        jira.add_issue("PROJ-1", "x", description="x", points=3, sprints=[{**S30, "state": "active"}])
        out = run(tmp_path, "--sprint", "Sprint 30", "--project", "PROJ", "--tracker", "jira")[1]
        assert out["load"]["pending_rollover"] is None and out["load"]["carry_over"]["count"] == 0

    def test_a_failed_rollover_lookup_is_reported_not_hidden(self, tmp_path, jira):
        self._sprint(jira)
        jira.add_board_sprint(7, {"id": 29, "name": "Sprint 29", "state": "active"})
        jira.fail("/rest/agile/1.0/board/7/sprint", 500, times=20, headers={"Retry-After": "0"}, method="GET")
        out = run(tmp_path, "--sprint", "Sprint 30", "--project", "PROJ", "--tracker", "jira")[1]
        assert out["load"]["pending_rollover"] is None and any("rollover could not be read" in n for n in out["load"]["notes"])

    def test_a_site_without_a_sprint_field_still_gets_lanes_and_says_carry_over_is_unknown(self, tmp_path, jira):
        jira.fields = [f for f in jira.fields if f["name"] != "Sprint"]
        jira.add_issue("PROJ-1", "x", description="x", points=3)
        out = run(tmp_path, "--sprint", "Sprint 30", "--project", "PROJ", "--tracker", "jira")[1]
        assert out["load"]["lanes"]["pending"]["points"] == 3.0 and out["load"]["carry_over"] is None and out["load"]["notes"]

    def test_an_all_finished_sprint_is_reported_not_called_empty(self, tmp_path, jira, capsys):
        jira.add_issue("PROJ-1", "x", description="x", points=3, status="Done", category="done", sprints=[S30])
        code, out = run(tmp_path, "--sprint", "Sprint 30", "--project", "PROJ", "--tracker", "jira")
        assert code == 0 and out["count"] == 1 and out["evaluated"] == 0 and out["load"]["lanes"]["done"]["points"] == 3.0

    def test_workflow_names_the_defaults_do_not_know_can_be_mapped(self, tmp_path, jira, monkeypatch):
        monkeypatch.setenv("SPRINT_LANES", '{"review": ["Ready to Ship"]}')
        jira.add_issue("PROJ-1", "x", description="x", points=3, status="Ready to Ship", category="indeterminate", sprints=[S30])
        assert run(tmp_path, "--sprint", "Sprint 30", "--project", "PROJ", "--tracker", "jira")[1]["load"]["lanes"]["review"]["points"] == 3.0


class TestGitHubSprintLoad:
    def test_lanes_come_from_state_and_workflow_labels(self, tmp_path, gh):
        gh.add_issue(1, "todo", body="x", labels=["sp:3"], milestone="M")
        gh.add_issue(2, "coding", body="x", labels=["sp:5", "in progress"], milestone="M")
        gh.add_issue(3, "reviewing", body="x", labels=["sp:8", "in review"], milestone="M")
        gh.add_issue(4, "done", body="x", labels=["sp:2"], milestone="M", state="CLOSED")
        gh.add_issue(5, "stuck", body="x", labels=["sp:1", "blocked"], milestone="M")
        gh.add_issue(6, "area label only", body="x", labels=["sp:1", "testing", "documentation"], milestone="M")
        out = run(tmp_path, "--milestone", "M", "--tracker", "github")[1]
        lanes = out["load"]["lanes"]
        assert [lanes[l]["points"] for l in ("pending", "in_progress", "review", "done")] == [3.0 + 1.0 + 1.0, 5.0, 8.0, 2.0]
        assert out["load"]["carry_over"] is None and any("GitHub" in n for n in out["load"]["notes"])

    def test_unpointed_issues_are_named(self, tmp_path, gh):
        gh.add_issue(1, "no points", body="x", milestone="M")
        assert run(tmp_path, "--milestone", "M", "--tracker", "github")[1]["load"]["lanes"]["pending"]["unpointed"] == ["#1"]

    def test_explicit_refs_have_no_load(self, tmp_path, gh):
        gh.add_issue(1, "t", body="x")
        assert run(tmp_path, "1", "--tracker", "github")[1]["load"] is None


class TestCredentialsCheck:
    def test_jira_ok_names_the_user_and_never_the_token(self, jira, capsys):
        assert fetch.main(["--check", "--tracker", "jira"]) == 0
        out = capsys.readouterr().out
        assert json.loads(out) == {"ok": True, "tracker": "jira", "user": "Test User"} and "test-token-123" not in out

    def test_bad_jira_credentials_are_blocked_with_a_clear_message(self, jira, capsys):
        jira.fail("/rest/api/3/myself", 401)
        assert fetch.main(["--check", "--tracker", "jira"]) == 3
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False and "do not work" in out["error"] and "test-token-123" not in json.dumps(out)

    def test_missing_jira_credentials_are_named_by_variable(self, jira, monkeypatch, capsys):
        monkeypatch.delenv("JIRA_API_TOKEN")
        assert fetch.main(["--check", "--tracker", "jira"]) == 3
        assert "JIRA_API_TOKEN" in capsys.readouterr().out

    def test_github_ok_and_failing(self, gh, capsys, monkeypatch):
        gh.set("whoami", "octocat")
        assert fetch.main(["--check", "--tracker", "github"]) == 0 and json.loads(capsys.readouterr().out)["user"] == "octocat"
        gh.set("whoami_fail", "HTTP 401: Bad credentials")
        assert fetch.main(["--check", "--tracker", "github"]) == 3
        assert "do not work" in capsys.readouterr().out


class TestTargetSprint:
    TICKETS = [{"sprints": [{"id": 30, "name": "Sprint 30"}, {"id": 31, "name": "Sprint 31"}]}, {"sprints": [{"id": 77, "name": "Sprint 30"}]}]

    def test_a_numeric_sprint_is_found_by_id(self):
        assert fetch.target_sprint(self.TICKETS, "31") == ({"id": 31, "name": "Sprint 31"}, "")

    def test_a_name_shared_by_two_sprints_is_ambiguous_and_says_so(self):
        target, note = fetch.target_sprint(self.TICKETS, "Sprint 30")
        assert target is None and "2 sprints are named 'Sprint 30'" in note and "30, 77" in note

    def test_an_unknown_sprint_is_none_without_a_note(self):
        assert fetch.target_sprint(self.TICKETS, "Sprint 99") == (None, "")
