"""Tests for scripts/qa/model.py and scripts/qa/delta.py."""

from __future__ import annotations

import json

import pytest

from scripts.qa import delta as dl
from scripts.qa import model as md


def rec(case, outcome, **kw):
    return {"case": case, "outcome": outcome, "message": kw.pop("message", ""), **kw}


def write_jsonl(path, records, garbage=False):
    text = "\n".join(json.dumps(r) for r in records)
    if garbage:
        text += '\n{"case": "C9", "outco'  # truncated by a killed process
    path.write_text(text)
    return path


class TestLoadAndCollapse:
    def test_truncated_line_is_skipped_not_fatal(self, tmp_path):
        p = write_jsonl(tmp_path / "r.jsonl", [rec("C1", "pass")], garbage=True)
        assert [r["case"] for r in md.load_jsonl(p)] == ["C1"]

    def test_missing_file_is_empty(self, tmp_path):
        assert md.load_jsonl(tmp_path / "nope") == []

    def test_unknown_outcome_becomes_inconclusive(self, tmp_path):
        p = write_jsonl(tmp_path / "r.jsonl", [rec("C1", "passed-ish")])
        assert md.load_jsonl(p)[0]["outcome"] == "inconclusive"

    def test_worst_outcome_wins_and_messages_kept(self):
        out = md.case_outcomes([rec("C1", "pass"), rec("C1", "fail", message="got 500"), rec("C1", "pass")])
        assert out["C1"]["outcome"] == "fail" and out["C1"]["messages"] == ["got 500"] and out["C1"]["checks"] == 3

    def test_blocked_beats_pass_but_not_fail(self):
        assert md.case_outcomes([rec("C", "pass"), rec("C", "blocked")])["C"]["outcome"] == "blocked"
        assert md.case_outcomes([rec("C", "blocked"), rec("C", "fail")])["C"]["outcome"] == "fail"

    def test_infra_flag_propagates(self):
        assert md.case_outcomes([rec("C", "blocked", infra=True)])["C"]["infra"] is True


PLAN = {"criteria": [{"id": "AC1", "text": "limit 10", "source": "issue:X-1"}, {"id": "AC2", "text": "error msg", "source": "pr_description"}],
        "cases": [{"id": "C1", "ac": ["AC1"], "type": "API", "expected": "409"},
                  {"id": "C2", "ac": ["AC1", "AC2"], "type": "UI", "expected": "banner"}]}


class TestPlan:
    def test_valid_plan(self):
        assert md.validate_plan(PLAN) == []

    def test_problems_reported(self):
        bad = {"criteria": [{"id": "AC1"}], "cases": [{"id": "C1", "ac": ["AC9"], "type": "FOO"}, {"id": "C1", "ac": [], "type": "API", "expected": "x"}]}
        problems = " | ".join(md.validate_plan(bad))
        assert "unknown criterion AC9" in problems and "type must be one of" in problems
        assert "no expected result" in problems and "duplicate case id C1" in problems
        assert md.validate_plan({}) == ["plan has no criteria", "plan has no test cases"]

    def test_coverage_lists_uncovered(self):
        plan = {**PLAN, "criteria": PLAN["criteria"] + [{"id": "AC3", "text": "x"}]}
        cov = md.coverage(plan)
        assert cov["uncovered"] == ["AC3"] and cov["by_criterion"]["AC1"] == ["C1", "C2"]

    def test_fitness_go_caution_nogo(self):
        assert md.fitness(PLAN, {"url": "http://x"})["verdict"] == "GO"
        plan = {**PLAN, "criteria": PLAN["criteria"] + [{"id": "AC3", "text": "x"}]}
        assert md.fitness(plan, {"url": "http://x"})["verdict"] == "CAUTION"
        assert md.fitness(PLAN, {})["verdict"] == "NO-GO"  # API/UI cases but no URL
        manual = {"criteria": [{"id": "A"}], "cases": [{"id": "C", "ac": ["A"], "type": "MANUAL", "expected": "x"}]}
        assert "no automatable case" in " ".join(md.fitness(manual, {"url": "u"})["blockers"])

    def test_fitness_flags_all_inferred_and_mutations(self):
        plan = {"criteria": [{"id": "A", "source": "inferred"}],
                "cases": [{"id": "C", "ac": ["A"], "type": "API", "expected": "x", "mutating": True}]}
        f = md.fitness(plan, {"url": "u"})
        assert f["verdict"] == "CAUTION" and len(f["gaps"]) == 2

    def test_ac_status(self):
        outs = {"C1": {"outcome": "pass"}, "C2": {"outcome": "fail"}}
        assert md.ac_status("AC1", PLAN, outs) == "partial"
        assert md.ac_status("AC2", PLAN, outs) == "not met"
        assert md.ac_status("AC1", PLAN, {"C1": {"outcome": "pass"}, "C2": {"outcome": "pass"}}) == "met"
        assert md.ac_status("AC1", PLAN, {"C1": {"outcome": "blocked"}, "C2": {"outcome": "not-run"}}) == "unverifiable"
        assert md.ac_status("AC1", PLAN, {"C1": {"outcome": "pass"}}) == "partial" or md.ac_status("AC1", PLAN, {"C1": {"outcome": "pass"}}) != "met"

    def test_summarize(self):
        s = md.summarize({"a": {"outcome": "pass"}, "b": {"outcome": "fail"}, "c": {"outcome": "blocked"}})
        assert s["executed"] == 2 and s["pass_rate"] == 0.5 and s["counts"]["blocked"] == 1
        assert md.summarize({})["pass_rate"] is None


def side(**cases):
    return md.case_outcomes([rec(k, v) for k, v in cases.items()])


class TestClassifyCase:
    @pytest.mark.parametrize("b,c,expected", [
        ("pass", "pass", "BOTH_PASS"), ("pass", "fail", "REGRESSION"), ("fail", "pass", "FIXED"), ("fail", "fail", "PRE_EXISTING"),
        ("blocked", "pass", "INFRA"), ("pass", "blocked", "INFRA"), ("not-run", "pass", "INCONCLUSIVE"), ("pass", "inconclusive", "INCONCLUSIVE"),
    ])
    def test_table(self, b, c, expected):
        assert dl.classify_case(side(X=b)["X"], side(X=c)["X"]) == expected

    def test_new_and_removed(self):
        assert dl.classify_case(None, {"outcome": "pass"}) == "NEW"
        assert dl.classify_case({"outcome": "pass"}, None) == "REMOVED"

    def test_blocked_infra_record_is_infra_not_regression(self):
        cand = md.case_outcomes([rec("X", "blocked", infra=True)])["X"]
        assert dl.classify_case(side(X="pass")["X"], cand) == "INFRA"

    def test_a_real_failure_is_never_masked_by_a_later_infra_blocked_check(self):
        """check 1 fails, then a later request in the same case gets no response: the failure still counts."""
        cand = md.case_outcomes([rec("X", "fail"), rec("X", "blocked", infra=True)])["X"]
        assert cand["outcome"] == "fail" and cand["infra"] is False
        assert dl.classify_case(side(X="pass")["X"], cand) == "REGRESSION"


class TestCompare:
    def test_regression_status(self):
        r = dl.compare([rec("A", "pass"), rec("B", "pass")], [rec("A", "pass"), rec("B", "fail")])
        assert r["status"] == "REGRESSION" and r["counts"] == {"BOTH_PASS": 1, "REGRESSION": 1}

    def test_clean_and_fixed(self):
        assert dl.compare([rec("A", "pass")], [rec("A", "pass")])["status"] == "CLEAN"
        assert dl.compare([rec("A", "fail")], [rec("A", "pass")])["status"] == "FIXED"

    def test_zero_executed_is_blocked_never_clean(self):
        r = dl.compare([], [])
        assert r["status"] == "BLOCKED" and "baseline" in r["reason"] and "candidate" in r["reason"]
        r = dl.compare([rec("A", "blocked", infra=True)], [rec("A", "blocked", infra=True)])
        assert r["status"] == "BLOCKED"

    def test_one_side_empty_is_blocked(self):
        assert dl.compare([rec("A", "pass")], [])["status"] == "BLOCKED"

    def test_infra_incomplete(self):
        r = dl.compare([rec("A", "pass"), rec("B", "pass")], [rec("A", "pass"), rec("B", "blocked")])
        assert r["status"] == "INCOMPLETE"

    @pytest.mark.parametrize("base,cand", [
        ([rec("A", "pass"), rec("B", "pass")], [rec("A", "pass"), rec("B", "not-run"), rec("C", "pass")]),     # inconclusive case
        ([rec("A", "pass"), rec("B", "pass")], [rec("A", "pass")]),                                          # case removed
    ])
    def test_clean_requires_every_case_to_be_comparable(self, base, cand):
        assert dl.compare(base, cand)["status"] == "INCOMPLETE"

    def test_new_case_failing_on_the_candidate_is_not_clean(self):
        r = dl.compare([rec("A", "pass")], [rec("A", "pass"), rec("N", "fail")])
        assert r["status"] == "NEW_FAILURES"
        assert dl.compare([rec("A", "pass")], [rec("A", "pass"), rec("N", "pass")])["status"] == "CLEAN"

    def test_failing_on_both_sides_is_reported_not_clean(self):
        assert dl.compare([rec("A", "fail")], [rec("A", "fail")])["status"] == "PRE_EXISTING"
        assert dl.compare([rec("A", "fail"), rec("B", "fail")], [rec("A", "fail"), rec("B", "pass")])["status"] == "PRE_EXISTING"

    def test_planned_cases_nobody_ran_are_incomplete_never_clean(self):
        r = dl.compare([rec("C1", "pass")], [rec("C1", "pass")], plan_ids={"C1", "C2", "C3"})
        assert r["status"] == "INCOMPLETE" and {row["case"]: row["delta"] for row in r["cases"]} == {"C1": "BOTH_PASS", "C2": "INCONCLUSIVE", "C3": "INCONCLUSIVE"}
        assert dl.compare([rec("C1", "pass")], [rec("C1", "pass")], plan_ids={"C1"})["status"] == "CLEAN"

    def test_only_planned_cases_count_as_executed(self):
        smoke = [rec("SMOKE1", "pass")]
        assert dl.compare(smoke, smoke)["status"] == "CLEAN"  # without a plan every case counts
        assert dl.compare(smoke, smoke, plan_ids={"C1"})["status"] == "BLOCKED"

    def test_regression_outranks_infra(self):
        r = dl.compare([rec("A", "pass"), rec("B", "pass")], [rec("A", "fail"), rec("B", "blocked")])
        assert r["status"] == "REGRESSION"


class TestLogs:
    def test_normalization_clusters_volatile_tokens(self):
        a = dl.normalize_log_line("2026-10-15T10:00:01Z ERROR conn 10.0.0.5:5432 failed id 3f2a9c1b-0000-4000-8000-123456789abc after 31ms")
        b = dl.normalize_log_line("2026-11-01T01:02:03Z ERROR conn 10.9.9.9:5433 failed id aaaaaaaa-1111-4222-8333-bbbbbbbbbbbb after 99ms")
        assert a == b

    def test_levels_and_noise(self):
        text = "INFO started\nERROR boom 1\nERROR boom 2\nWARN slow\nDEBUG error in debug\nGET /healthcheck ERROR ignored"
        c = dl.log_errors(text)
        assert sum(v["count"] for v in c.values()) == 3 and {v["severity"] for v in c.values()} == {"error", "warn"}

    def test_json_level_field_beats_keywords(self):
        text = '{"level":"info","msg":"no error here, error handling enabled"}\n{"level":"error","msg":"disk full"}'
        c = dl.log_errors(text)
        assert len(c) == 1 and list(c.values())[0]["severity"] == "error"

    def test_new_errors_only_in_candidate(self):
        base = "ERROR db timeout 1\nERROR cache miss storm"
        cand = "ERROR db timeout 2\nERROR new failure in handler\nERROR new failure in handler"
        found = dl.error_changes(base, cand)
        assert len(found) == 1 and found[0]["kind"] == "new" and found[0]["count"] == 2 and "new failure" in found[0]["sample"]

    def test_a_pattern_that_grows_is_flagged(self):
        base = "ERROR upstream reset\n"
        cand = "ERROR upstream reset\n" * 40
        found = dl.error_changes(base, cand)
        assert [(f["kind"], f["baseline_count"], f["count"]) for f in found] == [("increased", 1, 40)]
        assert dl.error_changes("ERROR upstream reset\n" * 3, "ERROR upstream reset\n" * 4) == []

    def test_status_codes_stay_distinguishable_but_other_numbers_do_not(self):
        assert dl.error_changes("ERROR request failed status 404", "ERROR request failed status 500")
        assert not dl.error_changes("ERROR request failed in 31ms status 500", "ERROR request failed in 4477ms status 500")

    def test_custom_noise(self):
        assert dl.error_changes("", "ERROR metadata service unreachable", noise=[r"metadata service"]) == []

    def test_noise_filter_does_not_swallow_real_errors_that_mention_health(self):
        assert dl.error_changes("", "ERROR health check failed for db")  # a real failure, not an access-log line
        assert dl.error_changes("", "ERROR failed to write trace")
        assert not dl.error_changes("", "ERROR GET /healthz HTTP/1.1 200 ok")

    def test_debug_and_info_lines_are_not_errors_even_when_they_mention_one(self):
        assert dl.log_errors("DEBUG error in debug\n2026-01-01T00:00:00Z INFO no error here\nTRACE failed?") == {}

    def test_json_levels(self):
        text = '{"level":"severe","msg":"disk full"}\n{"level":"debug","msg":"error detail"}\n{"level":"warning","msg":"slow"}'
        assert sorted(v["severity"] for v in dl.log_errors(text).values()) == ["error", "warn"]


class TestPerf:
    def test_flags_only_beyond_threshold(self):
        found = dl.perf_regressions({"/a": 100, "/b": 100, "/c": 0}, {"/a": 119, "/b": 130, "/c": 50, "/d": 5})
        assert [f["endpoint"] for f in found] == ["/b"] and found[0]["ratio"] == 1.3


class TestCli:
    def test_cli_writes_delta_and_marker(self, tmp_path, capsys):
        b = write_jsonl(tmp_path / "b.jsonl", [rec("A", "pass")])
        c = write_jsonl(tmp_path / "c.jsonl", [rec("A", "fail", message="500")])
        (tmp_path / "b.log").write_text("ERROR old")
        (tmp_path / "c.log").write_text("ERROR old\nERROR fresh")
        out = tmp_path / "delta.json"
        assert dl.main(["--baseline", str(b), "--candidate", str(c), "--baseline-log", str(tmp_path / "b.log"),
                        "--candidate-log", str(tmp_path / "c.log"), "--out", str(out)]) == 0
        data = json.loads(out.read_text())
        assert data["status"] == "REGRESSION" and [e["sample"] for e in data["new_log_errors"]] == ["ERROR fresh"]
        assert "QA_DELTA_STATUS::REGRESSION" in capsys.readouterr().out


class TestModelCli:
    def test_go_exit_zero(self, tmp_path, capsys):
        p = tmp_path / "plan.json"
        p.write_text(json.dumps(PLAN))
        assert md.main([str(p), "--url", "http://x"]) == 0
        assert json.loads(capsys.readouterr().out.split("::add-task-context")[0])["verdict"] == "GO"

    def test_nogo_exit_two(self, tmp_path):
        p = tmp_path / "plan.json"
        p.write_text(json.dumps(PLAN))
        assert md.main([str(p)]) == 2  # API/UI cases but no URL

    def test_unreadable_plan(self, tmp_path):
        assert md.main([str(tmp_path / "missing.json")]) == 2


class TestSplitByPlan:
    PLAN2 = {"cases": [{"id": "C1"}, {"id": "C2"}]}

    def test_unrecorded_planned_case_is_not_run_and_extras_are_separate(self):
        outs = md.case_outcomes([rec("C1", "pass"), rec("SMOKE1", "pass")])
        planned, extra = md.split_by_plan(self.PLAN2, outs)
        assert set(planned) == {"C1", "C2"} and planned["C2"]["outcome"] == "not-run" and set(extra) == {"SMOKE1"}
        assert md.summarize(planned)["executed"] == 1 and md.summarize(planned)["total"] == 2

    def test_coverage_tolerates_cases_without_ids(self):
        plan = {"criteria": [{"id": "A"}], "cases": [{"ac": ["A"], "type": "API", "expected": "x"}]}
        assert md.coverage(plan)["uncovered"] == ["A"]
        assert any("missing its id" in p for p in md.validate_plan(plan))
        assert md.fitness(plan, {"url": "u"})["verdict"] == "NO-GO"


class TestPlanAgainstTheChange:
    """The plan must exercise the kind of code the PR changed, and every case must be in a test file that will run."""

    UI_PLAN = {"criteria": [{"id": "AC1", "text": "t", "source": "issue:X-1"}],
               "cases": [{"id": "C1", "ac": ["AC1"], "type": "API", "title": "t", "expected": "e"}]}

    def test_ui_change_without_a_ui_case_is_no_go(self):
        r = md.fitness(self.UI_PLAN, {"url": "http://x", "changed_files": ["web/src/Button.tsx", "web/src/Button.test.tsx"]})
        assert r["verdict"] == "NO-GO" and any("UI" in b for b in r["blockers"])

    def test_ui_change_with_a_ui_case_is_fine(self):
        plan = {**self.UI_PLAN, "cases": [*self.UI_PLAN["cases"], {"id": "C2", "ac": ["AC1"], "type": "UI", "title": "t", "expected": "e"}]}
        assert md.fitness(plan, {"url": "http://x", "changed_files": ["web/src/Button.tsx"]})["verdict"] == "GO"

    def test_a_manual_ui_case_does_not_satisfy_the_ui_requirement(self):
        plan = {**self.UI_PLAN, "cases": [{"id": "C2", "ac": ["AC1"], "type": "MANUAL", "title": "t", "expected": "e"}, *self.UI_PLAN["cases"]]}
        assert md.fitness(plan, {"url": "http://x", "changed_files": ["ui/app.vue"]})["verdict"] == "NO-GO"

    def test_test_only_and_docs_changes_need_no_ui_case(self):
        files = ["web/src/__tests__/Button.test.tsx", "web/e2e/login.spec.ts", "README.md", "docs/ui.md"]
        assert md.fitness(self.UI_PLAN, {"url": "http://x", "changed_files": files})["verdict"] == "GO"

    def test_backend_change_with_only_ui_cases_is_a_gap(self):
        plan = {**self.UI_PLAN, "cases": [{"id": "C1", "ac": ["AC1"], "type": "UI", "title": "t", "expected": "e"}]}
        r = md.fitness(plan, {"url": "http://x", "changed_files": ["service/handler.go"]})
        assert r["verdict"] == "CAUTION" and any("backend" in g for g in r["gaps"])

    def test_a_backend_file_under_a_web_directory_is_backend_not_ui(self):
        assert md.classify_changes(["web/handler.go", "web/api/views.py", "web/src/app.ts"]) == {"ui": ["web/src/app.ts"], "backend": ["web/handler.go", "web/api/views.py"]}
        assert md.fitness(self.UI_PLAN, {"url": "http://x", "changed_files": ["web/handler.go"]})["verdict"] == "GO"

    def test_no_changed_files_means_no_requirement(self):
        assert md.fitness(self.UI_PLAN, {"url": "http://x"})["verdict"] == "GO"

    def test_a_case_missing_from_every_test_file_is_no_go(self):
        r = md.fitness(self.UI_PLAN, {"url": "http://x", "test_sources": {"api-tests.sh": "qa_case C2\nqa_summary"}})
        assert r["verdict"] == "NO-GO" and any("C1" in b for b in r["blockers"])

    def test_case_ids_match_whole_tokens_only(self):
        r = md.fitness(self.UI_PLAN, {"url": "http://x", "test_sources": {"api-tests.sh": "qa_case C10\n"}})
        assert r["verdict"] == "NO-GO"
        assert md.fitness(self.UI_PLAN, {"url": "http://x", "test_sources": {"api-tests.sh": "qa_case C10\nqa_case C1\n"}})["verdict"] == "GO"

    def test_manual_cases_need_no_test_file_entry(self):
        plan = {**self.UI_PLAN, "cases": [{"id": "C9", "ac": ["AC1"], "type": "MANUAL", "title": "t", "expected": "e"},
                                          {"id": "C1", "ac": ["AC1"], "type": "API", "title": "t", "expected": "e"}]}
        assert md.fitness(plan, {"url": "http://x", "test_sources": {"t.sh": "qa_case C1"}})["verdict"] == "GO"

    def test_cli_reads_changed_files_and_test_files_and_checks_shell_syntax(self, tmp_path, capsys):
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(self.UI_PLAN))
        ctx = tmp_path / "ctx.json"
        ctx.write_text(json.dumps({"changed_files": ["src/App.tsx"]}))
        sh = tmp_path / "api.sh"
        sh.write_text("qa_case C1\nif then\n")
        assert md.main([str(plan), "--url", "http://x", "--pr-context", str(ctx), "--tests", str(sh)]) == 2
        out = json.loads(capsys.readouterr().out.split("::add-task-context")[0])
        assert any("UI" in b for b in out["blockers"]) and any("syntax" in b for b in out["blockers"])
