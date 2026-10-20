"""Tests for scripts/tickets/readiness.py: statuses, evidence guard, actions, report, JSON and the final status line."""

from __future__ import annotations

import json

import pytest

from scripts.tickets import readiness as rd
from scripts.tickets.config import ReadinessConfig
from scripts.tickets.model import new_ticket, unreadable

FULL_TEXT = "## Why\nbecause\n## Acceptance Criteria\n- Given a, when b, then c. Test: make t\n- Given d, when e, then f. Test: make t\n- Given g, when h, then i. Test: make t\n"
POINTS = {"value": 3.0, "source": "Story Points", "known": True}
UNSET = {"value": None, "source": "Story Points", "known": True}


def ticket(id="PROJ-1", description=FULL_TEXT + "## Implementation Plan\nsteps", points=POINTS, **kw):
    return new_ticket(tracker="jira", id=id, url=f"https://jira.example/browse/{id}", title=f"title {id}", description=description,
                      status=kw.pop("status", "To Do"), status_category=kw.pop("status_category", "todo"), story_points=points, **kw)


def grade(id="PROJ-1", why="pass", scope="pass", acs="pass", dod="pass", variants="na", **kw):
    return {"id": id, "checks": {"why": why, "scope": scope, "acs": acs, "dod": dod, "variants": variants}, **kw}


def status(t, g=None, cfg=None, action=None):
    return rd.evaluate([t], {t["id"]: g} if g else {}, cfg or ReadinessConfig(), {t["id"]: action} if action else {})[0]["status"]


class TestStatuses:
    def test_everything_passing_is_ready(self):
        assert status(ticket(), grade()) == "READY"

    def test_unreadable_wins_over_everything(self):
        t = unreadable("jira", "PROJ-9", "not found or not visible")
        assert status(t, grade("PROJ-9", acs="missing")) == "UNREADABLE"

    def test_a_ticket_nobody_graded_is_never_ready(self):
        assert status(ticket()) == "UNGRADED"

    def test_blocked_label_or_link(self):
        assert status(ticket(labels=["blocked"]), grade()) == "BLOCKED"
        assert status(ticket(blocked_by=["PROJ-2"]), grade()) == "BLOCKED"

    def test_no_context_at_all_is_blocked(self):
        assert status(ticket(description="", points=UNSET), grade(why="missing", acs="missing")) == "BLOCKED"

    def test_missing_and_partial_acs(self):
        assert status(ticket(description="Make it nicer."), grade(acs="missing")) == "NEEDS_AC_WORK"
        assert status(ticket(), grade(acs="partial")) == "NEEDS_AC_CLARIFICATION"

    def test_acs_beat_points_but_points_are_noted(self):
        r = rd.evaluate([ticket(description="Make it nicer.", points=UNSET)], {"PROJ-1": grade(acs="missing")}, ReadinessConfig(), {})[0]
        assert r["status"] == "NEEDS_AC_WORK" and any("story points" in g for g in r["gaps"])

    @pytest.mark.parametrize("value", [None, 0.0, 4.0, 7.0, 21.0, 40.0])
    def test_unset_zero_off_scale_and_oversized_points_need_points(self, value):
        assert status(ticket(points={"value": value, "source": "x", "known": True}), grade()) == "NEEDS_STORY_POINTS"

    @pytest.mark.parametrize("value", [1.0, 2.0, 3.0, 5.0, 8.0, 13.0])
    def test_scale_values_pass(self, value):
        assert status(ticket(points={"value": value, "source": "x", "known": True}), grade()) == "READY"

    def test_the_scale_comes_from_the_config(self):
        t = ticket(points={"value": 4.0, "source": "x", "known": True})
        assert status(t, grade(), ReadinessConfig(points_scale=(1.0, 2.0, 4.0))) == "READY"

    def test_unreadable_points_are_a_concern_not_a_status(self):
        t = ticket(points={"value": None, "source": "field list unreadable", "known": False})
        r = rd.evaluate([t], {"PROJ-1": grade()}, ReadinessConfig(), {})[0]
        assert r["status"] == "READY" and any("could not be read" in c for c in r["concerns"])

    def test_missing_plan(self):
        assert status(ticket(description=FULL_TEXT), grade()) == "NEEDS_IMPL_PLAN"

    def test_variant_holes_only_count_when_the_project_defines_variants(self):
        g = grade(variants="partial", variant_holes=["single node"])
        assert status(ticket(), g) == "READY"
        assert status(ticket(), g, ReadinessConfig(variants=({"name": "single node"},))) == "HAS_VARIANT_HOLES"
        assert status(ticket(), grade(variants="pass"), ReadinessConfig(variants=({"name": "single node"},))) == "READY"

    def test_gaps_in_why_scope_and_dod_leave_the_ticket_ready_but_listed(self):
        r = rd.evaluate([ticket()], {"PROJ-1": grade(why="partial", scope="missing", dod="partial")}, ReadinessConfig(), {})[0]
        assert r["status"] == "READY" and len(r["gaps"]) == 3

    def test_sub_tasks(self):
        assert status(ticket(issue_type="Sub-task", is_subtask=True), grade(why="pass")) == "SUBTASK_OK"
        assert status(ticket(issue_type="Sub-task", is_subtask=True), grade(why="missing")) == "SUBTASK_NEEDS_CONTEXT"


class TestEvidenceGuard:
    def test_a_missing_grade_on_a_ticket_with_behavior_text_is_raised_to_partial(self):
        r = rd.evaluate([ticket()], {"PROJ-1": grade(acs="missing")}, ReadinessConfig(), {})[0]
        assert r["status"] == "NEEDS_AC_CLARIFICATION" and r["notes"]

    def test_alternate_spec_headings_count(self):
        t = ticket(description="## Outcome\nUsers see results in 2s\n## Implementation Plan\nx")
        assert status(t, grade(acs="missing")) == "NEEDS_AC_CLARIFICATION"

    def test_a_correct_missing_stays_missing(self):
        assert status(ticket(description="Make it nicer."), grade(acs="missing")) == "NEEDS_AC_WORK"


class TestActions:
    def test_written_acs_and_plan_turn_a_ticket_into_enriched(self):
        t = ticket(description="Make it nicer.")
        assert status(t, grade(acs="missing"), action="acs_written") == "NEEDS_IMPL_PLAN"
        r = rd.evaluate([t], {"PROJ-1": grade(acs="missing")}, ReadinessConfig(), {"PROJ-1": ["acs_written", "plan_written"]})[0]
        assert r["status"] == "ENRICHED"

    def test_acs_written_with_unset_points_still_needs_points(self):
        t = ticket(description="Make it nicer.", points=UNSET)
        assert status(t, grade(acs="missing"), action="acs_blocked_points") == "ENRICHED_PENDING_POINTS"

    def test_a_low_confidence_plan_does_not_loop(self):
        assert status(ticket(description=FULL_TEXT), grade(), action="plan_low_confidence") == "IMPL_PLAN_LOW_CONFIDENCE"

    def test_previews_and_skips_change_nothing(self):
        assert status(ticket(description=FULL_TEXT), grade(), action="preview") == "NEEDS_IMPL_PLAN"


class TestEnrichability:
    def test_only_agent_fixable_statuses_outside_no_enrich_states_are_auto_enrichable(self):
        tickets = [ticket("PROJ-1", description="Nice."), ticket("PROJ-2", status="In Review", status_category="in_progress", description="Nice."),
                   ticket("PROJ-3", points=UNSET)]
        grades = {t["id"]: grade(t["id"], acs="missing" if t["id"] != "PROJ-3" else "pass") for t in tickets}
        rows = {r["id"]: r for r in rd.evaluate(tickets, grades, ReadinessConfig(), {})}
        assert rows["PROJ-1"]["auto_enrichable"] and not rows["PROJ-2"]["auto_enrichable"] and not rows["PROJ-3"]["auto_enrichable"]
        assert rows["PROJ-2"]["in_flight"] and "In Review" in rows["PROJ-2"]["enrich_blocked"]


class TestSummaryAndStatusLine:
    def _summary(self, tickets, grades):
        rows = rd.evaluate(tickets, grades, ReadinessConfig(), {})
        return rd.summarize(rows), rd.status_line(rows)

    def test_all_ready_is_done(self):
        s, line = self._summary([ticket()], {"PROJ-1": grade()})
        assert s["ready"] == 1 and s["needs_human"] == 0 and line["status"] == "DONE" and line["signal"] == "DONE"

    def test_fixable_gaps_are_concerns(self):
        s, line = self._summary([ticket(description="Nice.")], {"PROJ-1": grade(acs="missing")})
        assert s["auto_enrichable"] == 1 and line["signal"] == "DONE_WITH_CONCERNS"

    def test_human_work_is_counted(self):
        s, line = self._summary([ticket(points=UNSET), ticket("PROJ-2", labels=["blocked"])], {"PROJ-1": grade(), "PROJ-2": grade("PROJ-2")})
        assert s["needs_human"] == 2 and line["needs_human"] == 2

    def test_nothing_readable_is_blocked(self):
        s, line = self._summary([unreadable("jira", "PROJ-1", "x")], {})
        assert line == {**line, "status": "ERROR", "signal": "BLOCKED"} and s["unreadable"] == 1

    def test_an_empty_ticket_list_is_blocked(self):
        assert rd.status_line([])["signal"] == "BLOCKED"


class TestGradeValidation:
    def test_unknown_grade_values_are_rejected(self):
        with pytest.raises(ValueError, match="acs"):
            rd.index_grades({"tickets": [grade(acs="great")]})

    def test_duplicate_and_unknown_ids_are_rejected_or_ignored_visibly(self):
        with pytest.raises(ValueError, match="duplicate"):
            rd.index_grades({"tickets": [grade(), grade()]})

    def test_extra_grades_are_reported(self):
        rows = rd.evaluate([ticket()], {"PROJ-1": grade(), "PROJ-7": grade("PROJ-7")}, ReadinessConfig(), {})
        assert rd.unknown_grade_ids([ticket()], {"PROJ-1": grade(), "PROJ-7": grade("PROJ-7")}) == ["PROJ-7"] and len(rows) == 1


class TestReport:
    def _report(self):
        tickets = [ticket("PROJ-1"), ticket("PROJ-2", description="Make search nicer.", status="In Progress", status_category="in_progress"),
                   ticket("PROJ-3", points=UNSET), unreadable("jira", "PROJ-4", "access denied", True),
                   ticket("PROJ-5", issue_type="Sub-task", is_subtask=True)]
        grades = {"PROJ-1": grade(), "PROJ-2": grade("PROJ-2", acs="partial", says="Make search nicer", questions=["Which fields does search match?"],
                                                     ac_starter="Given 10k users, when I search, then results show in 2s; Test: make perf",
                                                     evidence={"acs": "searched: Outcome; none"}),
                  "PROJ-3": grade("PROJ-3"), "PROJ-5": grade("PROJ-5", why="pass")}
        rows = rd.evaluate(tickets, grades, ReadinessConfig(), {})
        return rd.render_markdown(rows, {"tracker": "jira", "scope": "sprint X", "dry_run": True}), rows

    def test_verdict_first_then_cards_ready_subtasks_patterns_actions(self):
        md, _ = self._report()
        order = [md.index(h) for h in ("# Ticket readiness", "## Scoreboard", "## Needs attention", "## Ready", "## Sub-tasks", "## Patterns", "## Action items")]
        assert order == sorted(order)

    def test_a_card_quotes_the_ticket_and_asks_specific_questions(self):
        md, _ = self._report()
        card = md[md.index("### [PROJ-2]"):]
        assert "Make search nicer" in card and "Which fields does search match?" in card and "Given 10k users" in card
        assert "searched: Outcome; none" in card and "in-flight" in card

    def test_unreadable_tickets_get_an_action_item_and_no_grade(self):
        md, _ = self._report()
        assert "PROJ-4" in md and "access denied" in md

    def test_dry_run_is_stated(self):
        md, _ = self._report()
        assert "dry-run" in md.splitlines()[1]

    def test_table_cells_are_inert(self):
        t = ticket(description="x")
        t["title"] = "a | b <script>"
        md = rd.render_markdown(rd.evaluate([t], {"PROJ-1": grade(acs="missing")}, ReadinessConfig(), {}), {"tracker": "jira", "scope": "s", "dry_run": False})
        assert "<script>" not in md


def cli_files(tmp_path, tickets, grades, actions=None):
    return TestCli._files(None, tmp_path, tickets, grades, actions)


class TestCli:
    def _files(self, tmp_path, tickets, grades, actions=None):
        (tmp_path / "t.json").write_text(json.dumps({"tracker": "jira", "scope": "s", "tickets": tickets}))
        (tmp_path / "g.json").write_text(json.dumps({"tickets": grades}))
        args = ["--tickets", str(tmp_path / "t.json"), "--grades", str(tmp_path / "g.json"), "--out-dir", str(tmp_path / "reports")]
        if actions is not None:
            (tmp_path / "a.json").write_text(json.dumps(actions))
            args += ["--actions", str(tmp_path / "a.json")]
        return args

    def test_writes_the_bundle_and_prints_the_status_line_last(self, tmp_path, capsys):
        assert rd.main(self._files(tmp_path, [ticket()], [grade()])) == 0
        last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert last["signal"] == "DONE" and last["ready"] == 1
        for name in ("report.md", "report.html", "result.json"):
            assert (tmp_path / "reports" / name).exists()
        result = json.loads((tmp_path / "reports" / "result.json").read_text())
        assert result["summary"]["total"] == 1 and result["tickets"][0]["status"] == "READY"

    def test_actions_are_applied(self, tmp_path, capsys):
        args = self._files(tmp_path, [ticket(description="Nice.")], [grade(acs="missing")], [{"id": "PROJ-1", "action": "acs_written"}, {"id": "PROJ-1", "action": "plan_written"}])
        rd.main(args)
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ready"] == 0
        assert json.loads((tmp_path / "reports" / "result.json").read_text())["tickets"][0]["status"] == "ENRICHED"

    def test_nothing_readable_exits_blocked(self, tmp_path, capsys):
        assert rd.main(self._files(tmp_path, [unreadable("jira", "PROJ-1", "x")], [])) == 3
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["signal"] == "BLOCKED"

    def test_unreadable_inputs_and_bad_grades_are_usage_errors(self, tmp_path):
        assert rd.main(["--tickets", str(tmp_path / "nope.json"), "--grades", str(tmp_path / "nope.json"), "--out-dir", str(tmp_path)]) == 2
        assert rd.main(self._files(tmp_path, [ticket()], [grade(acs="great")])) == 2

    def test_credentials_in_ticket_text_never_reach_the_report(self, tmp_path):
        t = ticket(description="token ghp_abcdefghijklmnopqrstuvwxyz0123456789 leaked")
        rd.main(self._files(tmp_path, [t], [grade(acs="partial", says="token ghp_abcdefghijklmnopqrstuvwxyz0123456789")]))
        assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in (tmp_path / "reports" / "report.md").read_text()


class TestWriteResultsAreAccepted:
    """actions.json can be the JSON lines scripts.tickets.write printed, unchanged."""

    @pytest.mark.parametrize("item,canonical", [
        ({"kind": "acs", "action": "appended"}, "acs_written"), ({"kind": "acs", "action": "replaced"}, "acs_written"),
        ({"kind": "acs", "action": "exists"}, "acs_written"), ({"kind": "acs", "action": "blocked_points"}, "acs_blocked_points"),
        ({"kind": "plan", "action": "appended"}, "plan_written"), ({"kind": "plan", "action": "exists"}, "plan_written"),
        ({"kind": "plan", "action": "commented"}, "plan_low_confidence"), ({"kind": "acs", "action": "dry_run"}, "preview"),
        ({"kind": "plan", "action": "failed"}, "preview"), ({"action": "acs_written"}, "acs_written")])
    def test_mapping(self, item, canonical):
        assert rd.canonical_action(item) == canonical

    def test_cli_turns_write_results_into_enriched(self, tmp_path, capsys):
        t = ticket(description="Make it nicer.")
        files = cli_files(tmp_path, [t], [grade(acs="missing")], [
            {"id": "PROJ-1", "kind": "acs", "action": "appended", "verified": True}, {"id": "PROJ-1", "kind": "plan", "action": "appended"}])
        rd.main(files)
        assert json.loads((tmp_path / "reports" / "result.json").read_text())["tickets"][0]["status"] == "ENRICHED"


class TestMalformedInputs:
    def test_a_tickets_file_without_tickets_is_a_usage_error_not_a_traceback(self, tmp_path, capsys):
        (tmp_path / "t.json").write_text("{}")
        (tmp_path / "g.json").write_text('{"tickets": []}')
        assert rd.main(["--tickets", str(tmp_path / "t.json"), "--grades", str(tmp_path / "g.json"), "--out-dir", str(tmp_path / "r")]) == 2
        assert "tickets" in json.loads(capsys.readouterr().out)["error"]


class TestNoEnrichAndTruncation:
    def test_labels_and_finished_state_block_enrichment_on_trackers_without_workflow_states(self):
        tickets = [ticket("PROJ-1", description="Nice.", labels=["In Review"]), ticket("PROJ-2", description="Nice.", status="closed", status_category="done")]
        rows = {r["id"]: r for r in rd.evaluate(tickets, {t["id"]: grade(t["id"], acs="missing") for t in tickets}, ReadinessConfig(), {})}
        assert not rows["PROJ-1"]["auto_enrichable"] and "In Review" in rows["PROJ-1"]["enrich_blocked"]
        assert not rows["PROJ-2"]["auto_enrichable"] and "closed" in rows["PROJ-2"]["enrich_blocked"]

    def test_a_truncated_scope_is_a_concern_in_the_line_and_the_report(self, tmp_path, capsys):
        rows = rd.evaluate([ticket()], {"PROJ-1": grade()}, ReadinessConfig(), {})
        assert rd.status_line(rows)["signal"] == "DONE" and rd.status_line(rows, truncated=True)["signal"] == "DONE_WITH_CONCERNS"
        files = cli_files(tmp_path, [ticket()], [grade()])
        (tmp_path / "t.json").write_text(json.dumps({"tracker": "jira", "scope": "s", "truncated": True, "tickets": [ticket()]}))
        rd.main(files)
        assert "TRUNCATED" in (tmp_path / "reports" / "report.md").read_text()
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["signal"] == "DONE_WITH_CONCERNS"


class TestNothingUnreadableIsCalledClean:
    def test_story_points_that_could_not_be_read_stop_the_line_from_saying_everything_is_ready(self):
        t = ticket(points={"value": None, "source": "field list unreadable", "known": False})
        rows = rd.evaluate([t], {"PROJ-1": grade()}, ReadinessConfig(), {})
        line = rd.status_line(rows)
        assert rows[0]["status"] == "READY" and line["signal"] == "DONE_WITH_CONCERNS" and "could not be read" in line["summary"]

    def test_an_unreadable_parent_is_a_concern_on_its_sub_task(self):
        sub = ticket(issue_type="Sub-task", is_subtask=True, parent={"id": "PROJ-9", "title": "p", "description": "", "error": "the parent PROJ-9 could not be read: HTTP 403"})
        rows = rd.evaluate([sub], {"PROJ-1": grade(why="pass")}, ReadinessConfig(), {})
        assert rows[0]["status"] == "SUBTASK_OK" and rows[0]["concerns"] and rd.status_line(rows)["signal"] == "DONE_WITH_CONCERNS"

    def test_a_write_that_could_not_be_verified_changes_no_status(self):
        t = ticket(description="Nice.")
        rows = rd.evaluate([t], {"PROJ-1": grade(acs="missing")}, ReadinessConfig(), {"PROJ-1": [rd.canonical_action({"kind": "acs", "action": "written_unverified"})]})
        assert rows[0]["status"] == "NEEDS_AC_WORK"


class TestSlackSummary:
    """Slack gets a short mrkdwn summary (tables do not render there); the full cards live in the MD / HTML report."""

    def _rows(self, n=12):
        tickets = [ticket("PROJ-1")] + [ticket(f"PROJ-{i}", description="Make search nicer.", points=UNSET if i % 3 == 0 else POINTS) for i in range(2, n)]
        tickets += [unreadable("jira", f"PROJ-{n}", "access denied", True), ticket(f"PROJ-{n + 1}", labels=["blocked"])]
        grades = {"PROJ-1": grade()}
        grades.update({t["id"]: grade(t["id"], why="partial", scope="missing", acs="partial", dod="missing") for t in tickets[1:-2]})
        grades[f"PROJ-{n + 1}"] = grade(f"PROJ-{n + 1}")
        return rd.evaluate(tickets, grades, ReadinessConfig(), {})

    def test_it_is_short_has_no_tables_and_leads_with_the_verdict(self):
        text = rd.render_slack(self._rows(), {"tracker": "jira", "scope": "sprint Sprint 12", "dry_run": True})
        lines = text.splitlines()
        assert len(text) < 3500 and not any(ln.lstrip().startswith("|") or " | " in ln for ln in lines)   # no table rows (Slack's own <url|text> links are fine)
        assert lines[0].startswith("*Ticket readiness*") and "dry-run" in lines[0] and "13 ticket" in lines[0]
        assert "NEEDS_AC_CLARIFICATION" in text and "PROJ-13" in text   # the unreadable one is named

    def test_every_attention_ticket_gets_one_line_with_its_main_gaps_and_a_link(self):
        rows = self._rows()
        for r in rows:
            r["url"] = f"https://jira.example/browse/{r['id']}" if r["id"] != "PROJ-12" else ""
        text = rd.render_slack(rows, {"tracker": "jira", "scope": "s", "dry_run": False})
        assert "<https://jira.example/browse/PROJ-2|PROJ-2>" in text
        line = next(ln for ln in text.splitlines() if "PROJ-2>" in ln)
        assert "ACs partial" in line and "scope missing" in line and "\n" not in line

    def test_a_long_sprint_is_capped_and_points_at_the_full_report(self):
        text = rd.render_slack(self._rows(80), {"tracker": "jira", "scope": "s", "dry_run": False})
        assert len(text) < 6000 and "more not listed" in text and "full report" in text

    def test_slack_control_characters_in_ticket_text_are_neutralised(self):
        rows = rd.evaluate([ticket(description="x")], {"PROJ-1": grade(acs="missing")}, ReadinessConfig(), {})
        rows[0]["title"] = "<!channel> & <@U123> deploy"
        text = rd.render_slack(rows, {"tracker": "jira", "scope": "s", "dry_run": False})
        assert "<!channel>" not in text and "<@U123>" not in text and "&amp;" in text

    def test_the_cli_writes_it_next_to_the_report(self, tmp_path):
        rd.main(cli_files(tmp_path, [ticket()], [grade()]))
        assert "*Ticket readiness*" in (tmp_path / "reports" / "slack.md").read_text()

    def test_ticket_links_appear_in_the_markdown_report_too(self):
        t = ticket(description="Nice.")
        t["url"] = "https://jira.example/browse/PROJ-1"
        md = rd.render_markdown(rd.evaluate([t], {"PROJ-1": grade(acs="missing")}, ReadinessConfig(), {}), {"tracker": "jira", "scope": "s", "dry_run": False})
        assert "### [PROJ-1](https://jira.example/browse/PROJ-1)" in md

    def test_an_unreadable_ticket_card_has_no_dangling_separators(self):
        md = rd.render_markdown(rd.evaluate([unreadable("jira", "PROJ-9", "access denied", True)], {}, ReadinessConfig(), {}), {"tracker": "jira", "scope": "s", "dry_run": False})
        card = md[md.index("### PROJ-9"):]
        assert "(unreadable)" in card.splitlines()[0] and " ·  · " not in card


class TestSlackHardening:
    def _one(self, **overrides):
        rows = rd.evaluate([ticket(description="Nice.")], {"PROJ-1": grade(acs="missing")}, ReadinessConfig(), {})
        rows[0].update(overrides)
        return rd.render_slack(rows, {"tracker": "jira", "scope": "s", "dry_run": False})

    @pytest.mark.parametrize("url", ["https://e.com/><!channel|x", "https://e.com/a|b", "https://e.com/a b", "https://e.com/<x>", "javascript:alert(1)", "http://"])
    def test_a_url_that_could_break_slack_link_markup_is_not_linked(self, url):
        text = self._one(url=url)
        assert "<!channel" not in text and "<http" not in text.replace("<https://jira.example", "")
        assert "`PROJ-1`" in text

    def test_a_clean_url_is_linked(self):
        assert "<https://jira.example/browse/PROJ-1|PROJ-1>" in self._one()

    @pytest.mark.parametrize("field", ["id", "title", "fetch_error"])
    def test_mentions_in_any_ticket_field_cannot_ping(self, field):
        text = self._one(**{field: "x <!channel> <@U123> <!here|here>"})
        assert "<!channel>" not in text and "<@U123>" not in text and "<!here" not in text

    def test_a_secret_cut_by_a_length_limit_cannot_survive_as_a_fragment(self):
        secret = "ghp_" + "a1B2c3D4e5" * 4
        text = self._one(title="x" * 55 + " " + secret)
        assert "ghp_a1B2" not in text and secret not in text

    def test_entities_are_never_cut_in_half(self):
        text = self._one(title="&" * 80)
        for line in text.splitlines():
            if "PROJ-1" in line:
                assert "&amp" not in line.replace("&amp;", "")

    def test_one_huge_error_cannot_blow_up_the_message(self):
        rows = [dict(r, fetch_error="e" * 5000, status="UNREADABLE", id=f"P-{i}") for i, r in enumerate(rd.evaluate([ticket()] * 30, {}, ReadinessConfig(), {}))]
        text = rd.render_slack(rows, {"tracker": "jira", "scope": "s", "dry_run": False})
        assert len(text) < 8000 and all(len(ln) < 400 for ln in text.splitlines())

    def test_long_id_lists_say_how_many_were_left_out_instead_of_cutting_mid_id(self):
        rows = rd.evaluate([ticket(f"PROJ-{i}", description="Nice.") for i in range(1, 60)], {f"PROJ-{i}": grade(f"PROJ-{i}", acs="missing") for i in range(1, 60)}, ReadinessConfig(), {})
        text = rd.render_slack(rows, {"tracker": "jira", "scope": "s", "dry_run": False})
        status_line = next(ln for ln in text.splitlines() if ln.startswith("• NEEDS_AC_WORK"))
        assert "more)" in status_line and not status_line.rstrip().endswith("PROJ-")
        assert len(status_line) < 400

    def test_markdown_links_survive_a_url_with_parentheses(self):
        t = ticket(description="Nice.")
        t["url"] = "https://jira.example/browse/PROJ-1)(evil"
        md = rd.render_markdown(rd.evaluate([t], {"PROJ-1": grade(acs="missing")}, ReadinessConfig(), {}), {"tracker": "jira", "scope": "s", "dry_run": False})
        assert "](https://jira.example/browse/PROJ-1)(evil)" not in md and "### PROJ-1" in md   # not linked rather than broken


class TestActionsAndPreviews:
    def test_a_missing_actions_file_means_nothing_was_written(self, tmp_path, capsys):
        args = cli_files(tmp_path, [ticket()], [grade()]) + ["--actions", str(tmp_path / "does-not-exist.json")]
        assert rd.main(args) == 0
        assert "no actions" in capsys.readouterr().err.lower()

    def test_previews_written_for_a_ticket_are_included_in_the_report_and_announced_in_slack(self, tmp_path):
        files = cli_files(tmp_path, [ticket(description="Nice.")], [grade(acs="missing")])
        (tmp_path / "reports" / "PROJ-1").mkdir(parents=True)
        (tmp_path / "reports" / "PROJ-1" / "acs.md").write_text("## Acceptance Criteria (generated)\n- Given a draft, when it expires, then it is restored. Test: make t\n")
        (tmp_path / "reports" / "PROJ-1" / "plan.md").write_text("## Implementation Plan (generated)\n- change the middleware\n")
        rd.main(files)
        md = (tmp_path / "reports" / "report.md").read_text()
        assert "## Previews" in md and "then it is restored" in md and "change the middleware" in md
        assert "preview" in (tmp_path / "reports" / "slack.md").read_text().lower()

    def test_no_previews_no_section(self, tmp_path):
        rd.main(cli_files(tmp_path, [ticket()], [grade()]))
        assert "## Previews" not in (tmp_path / "reports" / "report.md").read_text()

    def test_preview_directories_of_unrelated_names_are_ignored(self, tmp_path):
        files = cli_files(tmp_path, [ticket()], [grade()])
        (tmp_path / "reports" / "OTHER-9").mkdir(parents=True)
        (tmp_path / "reports" / "OTHER-9" / "acs.md").write_text("stray")
        rd.main(files)
        assert "stray" not in (tmp_path / "reports" / "report.md").read_text()

    def test_story_point_gaps_and_the_scope_line_cannot_inject_slack_markup(self):
        t = ticket(description="Nice.", points={"value": None, "source": "Field <!channel> <@U1>", "known": True})
        rows = rd.evaluate([t], {"PROJ-1": grade(acs="missing")}, ReadinessConfig(), {})
        text = rd.render_slack(rows, {"tracker": "jira", "scope": "Sprint <!here>", "dry_run": False})
        assert "<!here>" not in text and "<!channel>" not in text and "<@U1>" not in text
