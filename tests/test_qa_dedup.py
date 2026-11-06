"""Tests for scripts/qa/dedup.py: repeated failures and exchanges are shown once with a count; counts and verdict never change."""

from scripts.qa import dedup, model


def rec(case, outcome="fail", message="", command="", observed="", **kw):
    return {"case": case, "outcome": outcome, "message": message, "command": command, "observed": observed, **kw}


class TestDedupFailures:
    def test_same_failure_across_cases_becomes_one_group_with_a_count(self):
        records = [rec(f"C{i}", message=f"GET /api/items returned 500 after {20 + i}ms at 2026-11-0{i}T10:00:00Z") for i in range(1, 6)]
        groups = dedup.dedup_failures(model.case_outcomes(records))
        assert len(groups) == 1
        g = groups[0]
        assert g["count"] == 5 and g["cases"] == ["C1", "C2", "C3", "C4", "C5"] and g["outcome"] == "fail"
        assert "500" in g["sample"]

    def test_different_failures_and_single_occurrences_are_not_grouped(self):
        records = [rec("C1", message="GET /a returned 500"), rec("C2", message="GET /b returned 404"), rec("C3", message="boom")]
        assert dedup.dedup_failures(model.case_outcomes(records)) == []

    def test_status_codes_keep_groups_apart(self):
        records = [rec("C1", message="request failed status 500"), rec("C2", message="request failed status 500"),
                   rec("C3", message="request failed status 404"), rec("C4", message="request failed status 404")]
        assert sorted(g["count"] for g in dedup.dedup_failures(model.case_outcomes(records))) == [2, 2]

    def test_blocked_and_failed_with_the_same_text_are_separate_groups(self):
        records = [rec("C1", message="connect refused"), rec("C2", message="connect refused"),
                   rec("C3", outcome="blocked", message="connect refused", infra=True), rec("C4", outcome="blocked", message="connect refused", infra=True)]
        assert sorted(g["outcome"] for g in dedup.dedup_failures(model.case_outcomes(records))) == ["blocked", "fail"]

    def test_passing_cases_never_appear_and_the_input_is_not_modified(self):
        records = [rec("C1", "pass"), rec("C2", "pass"), rec("C3", message="x"), rec("C4", message="x")]
        outcomes = model.case_outcomes(records)
        before = repr(outcomes)
        groups = dedup.dedup_failures(outcomes)
        assert [g["cases"] for g in groups] == [["C3", "C4"]] and repr(outcomes) == before

    def test_counts_are_unchanged_by_grouping(self):
        records = [rec(f"C{i}", message="same") for i in range(4)]
        outcomes = model.case_outcomes(records)
        dedup.dedup_failures(outcomes)
        assert model.summarize(outcomes)["counts"]["fail"] == 4


class TestDedupExchanges:
    def test_repeated_identical_exchange_in_one_case_is_collapsed_with_a_count(self):
        records = [rec("C1", command="GET /x", observed="HTTP 500 body") for _ in range(3)] + [rec("C1", command="GET /y", observed="HTTP 404")]
        out = dedup.dedup_exchanges(records)
        assert [(e["command"], e["count"]) for e in out] == [("GET /x", 3), ("GET /y", 1)]

    def test_the_same_exchange_in_different_cases_is_kept_per_case(self):
        records = [rec("C1", command="GET /x", observed="HTTP 401"), rec("C2", command="GET /x", observed="HTTP 401")]
        out = dedup.dedup_exchanges(records)
        assert [(e["case"], e["count"]) for e in out] == [("C1", 1), ("C2", 1)]

    def test_key_uses_only_the_first_240_chars_of_the_body_and_ignores_the_code_field(self):
        body = "a" * 240
        out = dedup.dedup_exchanges([rec("C1", command="GET /x", observed=body + "1"), rec("C1", command="GET /x", observed=body + "2")])
        assert len(out) == 1 and out[0]["count"] == 2

    def test_passing_records_are_skipped_by_default(self):
        assert dedup.dedup_exchanges([rec("C1", "pass", command="GET /x", observed="ok")]) == []


class TestClip:
    def test_long_text_is_clipped_with_the_number_of_hidden_characters(self):
        out = dedup.clip("x" * 1500, 1000)
        assert out.startswith("x" * 1000) and out.endswith("(500 more characters)")
        assert dedup.clip("short", 1000) == "short"
