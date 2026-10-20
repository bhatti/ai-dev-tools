"""Tests for scripts/tickets/write.py: idempotent, verified, non-destructive writes to Jira and GitHub tickets."""

from __future__ import annotations

import json

import pytest

from scripts.common import adf, jira_api
from scripts.tickets import jira as jira_adapter
from scripts.tickets import write
from scripts.tickets.model import TicketError
from scripts.tickets.sections import SECTIONS
from tests.fakes.gh_issues import FakeGhIssues
from tests.fakes.jira import FakeJira

ACS = "## Acceptance Criteria (generated)\n- Given a draft, when the session expires, then it is restored. Test: make test\n"
PLAN = "## Implementation Plan (generated)\n- change the session middleware\n"
ORIGINAL = "## Why\nCustomers lose drafts.\n\n| Env | Mode |\n"


@pytest.fixture()
def jira(monkeypatch):
    jira_api._field_id_cache.clear()
    with FakeJira(monkeypatch) as fake:
        fake.add_issue("PROJ-1", "title", description="## Why\nCustomers lose drafts.\n- keep this bullet", points=3)
        fake.issues["PROJ-1"]["fields"]["description"]["content"].insert(1, {"type": "table", "content": [{"type": "tableRow", "content": [
            {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Env"}]}]}]}]})
        yield fake


@pytest.fixture()
def gh(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_ORG", "acme")
    monkeypatch.setenv("GH_REPO", "widgets")
    return FakeGhIssues(tmp_path, monkeypatch).add_issue(1, "title", body="## Why\nCustomers lose drafts.\n", labels=["sp:3"])


def run(tmp_path, capsys, ref, kind="acs", text=ACS, *flags, tracker="jira"):
    f = tmp_path / "section.md"
    f.write_text(text)
    code = write.main(["--ref", ref, "--kind", kind, "--section-file", str(f), "--tracker", tracker, *flags])
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1])


class TestJira:
    def test_without_apply_nothing_is_written(self, tmp_path, capsys, jira):
        code, r = run(tmp_path, capsys, "PROJ-1")
        assert code == 0 and r["action"] == "dry_run" and r["would"] == "append" and not jira.writes()

    def test_apply_appends_one_section_and_keeps_everything_else(self, tmp_path, capsys, jira):
        before = json.loads(json.dumps(jira.issues["PROJ-1"]["fields"]["description"]))
        code, r = run(tmp_path, capsys, "PROJ-1", "acs", ACS, "--apply")
        after = jira.issues["PROJ-1"]["fields"]["description"]
        assert code == 0 and r["action"] == "appended" and r["verified"] is True
        assert after["content"][:len(before["content"])] == before["content"]  # untouched, including the table
        assert adf.has_section(after, "Acceptance Criteria (generated)") and "restored" in jira.description_text("PROJ-1")

    def test_a_second_run_does_not_duplicate(self, tmp_path, capsys, jira):
        run(tmp_path, capsys, "PROJ-1", "acs", ACS, "--apply")
        code, r = run(tmp_path, capsys, "PROJ-1", "acs", ACS, "--apply")
        assert code == 0 and r["action"] == "exists"
        assert jira.description_text("PROJ-1").count("Acceptance Criteria (generated)") == 1

    def test_force_replaces_only_that_section(self, tmp_path, capsys, jira):
        run(tmp_path, capsys, "PROJ-1", "acs", ACS, "--apply")
        run(tmp_path, capsys, "PROJ-1", "plan", PLAN, "--apply")
        code, r = run(tmp_path, capsys, "PROJ-1", "acs", ACS.replace("restored", "kept"), "--apply", "--force")
        text = jira.description_text("PROJ-1")
        assert code == 0 and r["action"] == "replaced" and "kept" in text and "restored" not in text and "change the session middleware" in text
        assert "keep this bullet" in text

    def test_acs_are_not_written_while_points_are_unset(self, tmp_path, capsys, jira):
        jira.issues["PROJ-1"]["fields"]["customfield_10016"] = None
        code, r = run(tmp_path, capsys, "PROJ-1", "acs", ACS, "--apply")
        assert code == 3 and r["action"] == "blocked_points" and not jira.writes()
        code, r = run(tmp_path, capsys, "PROJ-1", "acs", ACS, "--apply", "--allow-unpointed")
        assert code == 0 and r["action"] == "appended"

    def test_a_plan_does_not_need_points(self, tmp_path, capsys, jira):
        jira.issues["PROJ-1"]["fields"]["customfield_10016"] = None
        assert run(tmp_path, capsys, "PROJ-1", "plan", PLAN, "--apply")[1]["action"] == "appended"

    def test_unreadable_points_block_acs_instead_of_guessing(self, tmp_path, capsys, jira):
        jira.fail("/rest/api/3/field", 403)
        code, r = run(tmp_path, capsys, "PROJ-1", "acs", ACS, "--apply")
        assert code == 3 and r["action"] == "blocked_points" and "could not be read" in r["detail"]

    def test_a_failed_write_leaves_the_ticket_alone_and_falls_back_to_a_comment(self, tmp_path, capsys, jira):
        jira.fail("/rest/api/3/issue/PROJ-1", 500, times=20, headers={"Retry-After": "0"}, method="PUT")
        before = jira.issues["PROJ-1"]["fields"]["description"]
        code, r = run(tmp_path, capsys, "PROJ-1", "plan", PLAN, "--apply")
        assert code == 1 and r["action"] == "failed" and jira.issues["PROJ-1"]["fields"]["description"] == before
        assert any(p.endswith("/comment") for _, p, _ in jira.writes()) and "comment instead" in r["detail"]

    def test_comment_only_posts_a_comment_and_never_touches_the_description(self, tmp_path, capsys, jira):
        before = jira.description_text("PROJ-1")
        code, r = run(tmp_path, capsys, "PROJ-1", "plan", "Not enough evidence to propose a plan.", "--apply", "--comment-only")
        assert code == 0 and r["action"] == "commented" and jira.description_text("PROJ-1") == before
        assert any(p.endswith("/comment") for _, p, _ in jira.writes())

    def test_secrets_in_the_text_are_redacted_before_writing(self, tmp_path, capsys, jira):
        run(tmp_path, capsys, "PROJ-1", "plan", PLAN + "- token ghp_abcdefghijklmnopqrstuvwxyz0123456789\n", "--apply")
        assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in jira.description_text("PROJ-1")

    def test_a_missing_ticket_is_blocked(self, tmp_path, capsys, jira):
        code, r = run(tmp_path, capsys, "PROJ-404", "plan", PLAN, "--apply")
        assert code == 3 and r["action"] == "unreadable"

    def test_a_ticket_without_a_description_gets_a_new_document(self, tmp_path, capsys, jira):
        jira.add_issue("PROJ-2", "empty", description=None, points=2)
        assert run(tmp_path, capsys, "PROJ-2", "acs", ACS, "--apply")[1]["action"] == "appended"
        assert "restored" in jira.description_text("PROJ-2")


class TestGitHub:
    def test_apply_appends_marked_block_and_keeps_the_body(self, tmp_path, capsys, gh):
        code, r = run(tmp_path, capsys, "1", "acs", ACS, "--apply", tracker="github")
        body = gh.body(1)
        assert code == 0 and r["action"] == "appended" and r["verified"] is True
        assert body.startswith("## Why\nCustomers lose drafts.") and "<!-- ygs:acceptance-criteria:start -->" in body and "restored" in body

    def test_dry_run_does_not_call_edit(self, tmp_path, capsys, gh):
        run(tmp_path, capsys, "1", "acs", ACS, tracker="github")
        assert not gh.calls_matching("issue edit")

    def test_idempotent_and_forceable(self, tmp_path, capsys, gh):
        run(tmp_path, capsys, "1", "acs", ACS, "--apply", tracker="github")
        assert run(tmp_path, capsys, "1", "acs", ACS, "--apply", tracker="github")[1]["action"] == "exists"
        code, r = run(tmp_path, capsys, "1", "acs", ACS.replace("restored", "kept"), "--apply", "--force", tracker="github")
        assert r["action"] == "replaced" and gh.body(1).count("ygs:acceptance-criteria:start") == 1 and "kept" in gh.body(1)

    def test_points_gate_uses_labels(self, tmp_path, capsys, gh):
        gh.add_issue(2, "no points", body="x")
        code, r = run(tmp_path, capsys, "2", "acs", ACS, "--apply", tracker="github")
        assert code == 3 and r["action"] == "blocked_points" and gh.body(2) == "x"

    def test_failed_edit_falls_back_to_a_comment(self, tmp_path, capsys, gh):
        gh.set("edit_fail", "HTTP 403: Resource not accessible")
        code, r = run(tmp_path, capsys, "1", "plan", PLAN, "--apply", tracker="github")
        assert code == 1 and r["action"] == "failed" and gh.body(1).startswith("## Why") and "Implementation Plan" in gh.comments(1)[0]

    def test_url_refs_work(self, tmp_path, capsys, gh):
        code, r = run(tmp_path, capsys, "https://github.com/acme/widgets/issues/1", "plan", PLAN, "--apply", tracker="github")
        assert code == 0 and r["id"] == "#1"


class TestArguments:
    def test_empty_or_missing_section_files_are_usage_errors(self, tmp_path, capsys):
        f = tmp_path / "s.md"
        f.write_text("   \n")
        assert write.main(["--ref", "PROJ-1", "--kind", "acs", "--section-file", str(f)]) == 2
        assert write.main(["--ref", "PROJ-1", "--kind", "acs", "--section-file", str(tmp_path / "nope.md")]) == 2

    def test_force_and_comment_only_exclude_each_other(self, tmp_path):
        f = tmp_path / "s.md"
        f.write_text("x")
        assert write.main(["--ref", "PROJ-1", "--kind", "plan", "--section-file", str(f), "--force", "--comment-only"]) == 2



class TestSectionBoundaryAndUserContent:
    def test_force_never_touches_text_a_person_added_around_the_section(self, tmp_path, capsys, jira):
        run(tmp_path, capsys, "PROJ-1", "acs", ACS, "--apply")
        doc = jira.issues["PROJ-1"]["fields"]["description"]
        doc["content"].append({"type": "paragraph", "content": [{"type": "text", "text": "A PERSON ADDED THIS AFTER"}]})
        doc["content"].append({"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "their own sub heading"}]})
        code, r = run(tmp_path, capsys, "PROJ-1", "acs", ACS.replace("restored", "kept"), "--apply", "--force")
        text = jira.description_text("PROJ-1")
        assert code == 0 and r["action"] == "replaced" and "A PERSON ADDED THIS AFTER" in text and "their own sub heading" in text and "kept" in text

    def test_generated_text_with_its_own_headings_and_rules_is_replaced_cleanly(self, tmp_path, capsys, jira):
        messy = PLAN + "\n## Risks\n- r1\n\n---\nafter the rule\n"
        run(tmp_path, capsys, "PROJ-1", "plan", messy, "--apply")
        code, r = run(tmp_path, capsys, "PROJ-1", "plan", PLAN.replace("middleware", "service"), "--apply", "--force")
        text = jira.description_text("PROJ-1")
        assert code == 0 and "service" in text and "r1" not in text and "after the rule" not in text and "keep this bullet" in text

    def test_a_heading_a_person_wrote_is_never_replaced(self, tmp_path, capsys, jira):
        jira.issues["PROJ-1"]["fields"]["description"]["content"] += adf.from_markdown("## Acceptance Criteria (generated)\n- written by hand\n")
        before = jira.description_text("PROJ-1")
        code, r = run(tmp_path, capsys, "PROJ-1", "acs", ACS, "--apply", "--force")
        assert code == 1 and r["action"] == "failed" and "not a section written by this tool" in r["detail"] and jira.description_text("PROJ-1") == before

    def test_marker_text_inside_the_content_is_refused(self, tmp_path, capsys, gh):
        f = tmp_path / "s.md"
        f.write_text("- x\n<!-- ygs:acceptance-criteria:end -->\n- y")
        assert write.main(["--ref", "1", "--kind", "acs", "--section-file", str(f), "--tracker", "github", "--apply"]) == 2
        assert gh.body(1) == "## Why\nCustomers lose drafts.\n"


class TestPolicyIsEnforcedByTheTool:
    @pytest.mark.parametrize("status,category", [("In Review", "indeterminate"), ("Done", "done")])
    def test_in_review_and_finished_tickets_are_not_rewritten(self, tmp_path, capsys, jira, status, category):
        jira.add_issue("PROJ-5", "t", description="x", points=3, status=status, category=category)
        code, r = run(tmp_path, capsys, "PROJ-5", "plan", PLAN, "--apply")
        assert code == 3 and r["action"] == "ineligible" and not jira.writes()

    def test_blocked_tickets_and_sub_tasks_are_not_rewritten(self, tmp_path, capsys, jira):
        jira.add_issue("PROJ-5", "t", description="x", points=3, labels=["blocked"])
        jira.add_issue("PROJ-6", "t", description="x", issue_type="Sub-task", parent="PROJ-1")
        assert run(tmp_path, capsys, "PROJ-5", "plan", PLAN, "--apply")[1]["action"] == "ineligible"
        assert run(tmp_path, capsys, "PROJ-6", "plan", PLAN, "--apply")[1]["action"] == "ineligible"

    def test_a_person_can_override_explicitly(self, tmp_path, capsys, jira):
        jira.add_issue("PROJ-5", "t", description="x", points=3, status="In Review", category="indeterminate")
        assert run(tmp_path, capsys, "PROJ-5", "plan", PLAN, "--apply", "--allow-ineligible")[1]["action"] == "appended"

    def test_github_has_no_workflow_states_so_labels_and_closed_state_decide(self, tmp_path, capsys, gh):
        gh.add_issue(2, "t", body="x", labels=["sp:3", "In Review"])
        gh.add_issue(3, "t", body="x", labels=["sp:3"], state="CLOSED")
        assert run(tmp_path, capsys, "2", "plan", PLAN, "--apply", tracker="github")[1]["action"] == "ineligible"
        assert run(tmp_path, capsys, "3", "plan", PLAN, "--apply", tracker="github")[1]["action"] == "ineligible"

    def test_comments_are_still_allowed_on_ineligible_tickets(self, tmp_path, capsys, jira):
        jira.add_issue("PROJ-5", "t", description="x", points=3, status="Done", category="done")
        assert run(tmp_path, capsys, "PROJ-5", "plan", "Not enough evidence.", "--apply", "--comment-only")[1]["action"] == "commented"

    def test_an_existing_plan_in_any_form_is_not_duplicated(self, tmp_path, capsys, jira, gh):
        jira.add_issue("PROJ-5", "t", description="## Implementation plan\n- do the thing", points=3)
        code, r = run(tmp_path, capsys, "PROJ-5", "plan", PLAN, "--apply")
        assert code == 0 and r["action"] == "exists" and "implementation plan" in r["detail"] and not jira.writes()
        gh.add_issue(4, "t", body="<!-- ai-plan:start -->\n## Implementation Plan\nold\n<!-- ai-plan:end -->", labels=["sp:3"])
        assert run(tmp_path, capsys, "4", "plan", PLAN, "--apply", tracker="github")[1]["action"] == "exists"

    def test_comment_only_is_idempotent_and_keeps_its_structure(self, tmp_path, capsys, jira):
        text = "## Why no plan\n- the entry point is unclear"
        assert run(tmp_path, capsys, "PROJ-1", "plan", text, "--apply", "--comment-only")[1]["action"] == "commented"
        again = run(tmp_path, capsys, "PROJ-1", "plan", text, "--apply", "--comment-only")[1]
        assert again["action"] == "commented" and "already posted" in again["detail"]   # still reads as the low-confidence outcome
        assert sum(1 for _, p, _ in jira.writes() if p.endswith("/comment")) == 1


class TestSafetyNets:
    def test_a_write_that_does_not_read_back_as_written_is_reported_unverified_without_a_second_copy(self, tmp_path, capsys, jira, monkeypatch):
        original = jira_adapter.JiraTickets.read_parts
        calls = []

        def drifting(self, ref, section):
            outside, inside = original(self, ref, section)
            calls.append(1)
            return (outside + " something changed", inside) if len(calls) > 1 else (outside, inside)
        monkeypatch.setattr(jira_adapter.JiraTickets, "read_parts", drifting)
        code, r = run(tmp_path, capsys, "PROJ-1", "plan", PLAN, "--apply")
        assert code == 1 and r["action"] == "written_unverified" and "did not match" in r["detail"]
        assert not any(p.endswith("/comment") for _, p, _ in jira.writes())   # the text is already on the ticket: no duplicate comment

    def test_a_ticket_that_changed_between_the_check_and_the_write_is_not_written(self, tmp_path, jira):
        client = jira_adapter.JiraTickets({"JIRA_BASE_URL": jira.base, "JIRA_EMAIL": "t@example.com", "JIRA_API_TOKEN": "x"})
        client.write_section("PROJ-1", SECTIONS["plan"], PLAN, replace=False)   # someone else appended it first
        with pytest.raises(TicketError, match="changed while"):
            client.write_section("PROJ-1", SECTIONS["plan"], PLAN, replace=False)
        assert jira.description_text("PROJ-1").count("Implementation Plan (generated)") == 1

    def test_formatting_in_generated_text_survives_as_formatting(self, tmp_path, capsys, jira):
        run(tmp_path, capsys, "PROJ-1", "plan", "## Implementation Plan (generated)\n- **Approach:** use `Session` from [the docs](https://example.com/d)\n  - nested step\n", "--apply")
        panel = jira.issues["PROJ-1"]["fields"]["description"]["content"][-1]
        assert panel["type"] == "panel"
        item = panel["content"][1]["content"][0]["content"]
        assert item[0]["content"][0]["marks"] == [{"type": "strong"}] and item[1]["type"] == "bulletList"
        assert "**" not in jira.description_text("PROJ-1")

    def test_redaction_is_reported_when_it_changes_the_text(self, tmp_path, capsys, jira):
        code, r = run(tmp_path, capsys, "PROJ-1", "plan", PLAN + "- token ghp_abcdefghijklmnopqrstuvwxyz0123456789\n", "--apply")
        assert r["redacted"] is True and "redacted" in r["detail"].lower()
        assert "redacted" not in run(tmp_path, capsys, "PROJ-1", "plan", PLAN, "--apply", "--force")[1]

    def test_too_large_a_github_body_is_refused_before_writing(self, tmp_path, capsys, gh):
        code, r = run(tmp_path, capsys, "1", "plan", "- " + "x" * 70000, "--apply", tracker="github")
        assert code == 1 and r["action"] == "failed" and "65536" in r["detail"] and gh.body(1) == "## Why\nCustomers lose drafts.\n"


class TestVerificationFollowsTheRenderedText:
    """Verification compares what a reader sees, so markdown that renders differently from its raw text still verifies."""

    @pytest.mark.parametrize("body", ["1. a\n1. b\n1. c", "5. a\n7. b", "- see [http://x.example/a](http://x.example/a)", "- **a**b and `id`s", "- [ ] todo\n- [x] done",
                                       "> quoted\n\n| Env | Mode |\n| --- | --- |\n| one | two |"])
    def test_awkward_markdown_still_verifies(self, tmp_path, capsys, jira, body):
        code, r = run(tmp_path, capsys, "PROJ-1", "plan", f"## Implementation Plan (generated)\n{body}\n", "--apply")
        assert code == 0 and r["action"] == "appended" and r["verified"] is True

    def test_the_skill_templates_heading_is_not_duplicated(self, tmp_path, capsys, jira, gh):
        template = "## Implementation Plan\n**Approach:** one\n"
        run(tmp_path, capsys, "PROJ-1", "plan", template, "--apply")
        assert jira.description_text("PROJ-1").count("Implementation Plan") == 1
        run(tmp_path, capsys, "1", "plan", template, "--apply", tracker="github")
        assert gh.body(1).count("Implementation Plan") == 1

    def test_a_dry_run_already_knows_a_human_written_heading_cannot_be_replaced(self, tmp_path, capsys, jira):
        jira.issues["PROJ-1"]["fields"]["description"]["content"] += adf.from_markdown("## Acceptance Criteria (generated)\n- by hand\n")
        code, r = run(tmp_path, capsys, "PROJ-1", "acs", ACS, "--force")
        assert code == 1 and r["action"] == "failed" and "a person wrote" in r["detail"] and not jira.writes()

    def test_a_panel_a_person_made_with_the_same_heading_is_never_replaced_by_force(self, tmp_path, capsys, jira):
        person = {"type": "panel", "attrs": {"panelType": "info"}, "content": adf.from_markdown("## Acceptance Criteria (generated)\n- HUMAN EDITS\n")}
        jira.issues["PROJ-1"]["fields"]["description"]["content"].append(person)
        code, r = run(tmp_path, capsys, "PROJ-1", "acs", ACS, "--apply", "--force")
        assert code == 1 and "HUMAN EDITS" in jira.description_text("PROJ-1")

    def test_a_stray_start_marker_does_not_swallow_a_persons_text(self, tmp_path, capsys, gh):
        gh.add_issue(7, "t", body="intro\n<!-- ygs:acceptance-criteria:start -->\nPERSON TEXT\n", labels=["sp:3"])
        run(tmp_path, capsys, "7", "acs", ACS, "--apply", tracker="github")
        code, r = run(tmp_path, capsys, "7", "acs", ACS.replace("restored", "kept"), "--apply", "--force", tracker="github")
        assert code == 0 and r["action"] == "replaced" and "PERSON TEXT" in gh.body(7) and "kept" in gh.body(7)
