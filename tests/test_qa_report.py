"""Tests for scripts/qa/report.py."""

from __future__ import annotations

import json

from scripts.qa import delta as dl
from scripts.common.report_bundle import collect_evidence as collect_evidence_for
from scripts.qa import report as rp

PLAN = {
    "criteria": [{"id": "AC1", "text": "The cart rejects an 11th item with HTTP 409", "source": "issue:SHOP-7"},
                 {"id": "AC2", "text": "The UI shows the limit message", "source": "pr_description"}],
    "cases": [{"id": "C1", "title": "11th item", "type": "API", "ac": ["AC1"], "expected": "409"},
              {"id": "C2", "title": "limit banner", "type": "UI", "ac": ["AC2"], "expected": "banner"}],
}


def rec(case, outcome, message="", **kw):
    return {"case": case, "outcome": outcome, "message": message, "command": kw.pop("command", "curl -X GET /x"),
            "observed": kw.pop("observed", "HTTP 200"), "screenshot": kw.pop("screenshot", ""), "infra": kw.pop("infra", False), **kw}


def assess(records, delta=None, env_failures=(), findings=None, plan=PLAN):
    return rp.assess(plan, records, delta, list(env_failures), findings or {})


class TestAssess:
    def test_all_pass_is_ready(self):
        a = assess([rec("C1", "pass"), rec("C2", "pass")])
        assert a["readiness"] == "Ready" and a["ac_status"] == {"AC1": "met", "AC2": "met"}

    def test_zero_executed_is_blocked_never_ready(self):
        a = assess([])
        assert a["readiness"] == "Blocked" and any("no planned case executed" in b for b in a["blockers"])
        a = assess([rec("C1", "blocked", infra=True), rec("C2", "blocked", infra=True)])
        assert a["readiness"] == "Blocked"

    def test_failure_is_not_ready_and_criterion_not_met(self):
        a = assess([rec("C1", "fail", "got 200"), rec("C2", "pass")])
        assert a["readiness"] == "Not ready" and a["ac_status"]["AC1"] == "not met"

    def test_blocked_case_downgrades_to_concerns(self):
        a = assess([rec("C1", "pass"), rec("C2", "blocked", "no browser")])
        assert a["readiness"] == "Ready with concerns" and a["ac_status"]["AC2"] == "unverifiable"
        assert any("blocked" in c for c in a["concerns"])

    def test_uncovered_criterion_is_a_concern(self):
        plan = {**PLAN, "criteria": PLAN["criteria"] + [{"id": "AC3", "text": "x", "source": "issue:SHOP-7"}]}
        a = assess([rec("C1", "pass"), rec("C2", "pass")], plan=plan)
        assert a["readiness"] == "Ready with concerns" and any("AC3" in c for c in a["concerns"])

    def test_inferred_criteria_cap_at_concerns(self):
        plan = {"criteria": [{"id": "AC1", "text": "t", "source": "inferred"}], "cases": [PLAN["cases"][0]]}
        assert assess([rec("C1", "pass")], plan=plan)["readiness"] == "Ready with concerns"

    def test_scope_creep_is_a_concern_not_a_failure(self):
        a = assess([rec("C1", "pass"), rec("C2", "pass")], findings={"scope_creep": [{"behavior": "new export endpoint"}]})
        assert a["readiness"] == "Ready with concerns"

    def test_env_failure_blocks(self):
        a = assess([rec("C1", "pass"), rec("C2", "pass")], env_failures=[{"side": "baseline", "error": "docker daemon unreachable"}])
        assert a["readiness"] == "Blocked" and "baseline environment failed" in a["blockers"][0]

    def test_regression_delta_makes_not_ready_even_if_candidate_passes(self):
        delta = dl.compare([rec("C1", "pass"), rec("C2", "pass")], [rec("C1", "fail"), rec("C2", "pass")])
        a = assess([rec("C1", "pass"), rec("C2", "pass")], delta=delta)
        assert a["readiness"] == "Not ready"

    def test_blocked_delta_blocks(self):
        delta = dl.compare([], [rec("C1", "pass")])
        assert assess([rec("C1", "pass"), rec("C2", "pass")], delta=delta)["readiness"] == "Blocked"

    def test_new_log_errors_and_perf(self):
        delta = {"status": "CLEAN", "reason": "x", "cases": [], "new_log_errors": [{"count": 2, "sample": "ERROR x"}],
                 "perf_regressions": [{"endpoint": "/a", "baseline_p50_ms": 10, "candidate_p50_ms": 30, "ratio": 3.0}]}
        a = assess([rec("C1", "pass"), rec("C2", "pass")], delta=delta)
        assert a["readiness"] == "Not ready" and any("slower" in p for p in a["problems"]) and any("new or increased error" in c for c in a["concerns"])


class TestMarkdown:
    def md(self, records, delta=None, findings=None, links=None, **meta):
        a = assess(records, delta, findings=findings)
        return rp.render_markdown(PLAN, a, {"title": "Add cart limit", "mode": "verify", "target_url": "http://x", **meta}, delta, findings or {}, links)

    def test_traceability_is_verbatim_with_cases_and_status(self):
        md = self.md([rec("C1", "pass"), rec("C2", "fail", "banner missing", screenshot="shots/c2.png")], links={"shots/c2.png": "evidence/c2.png"})
        assert "| AC1 | The cart rejects an 11th item with HTTP 409 | issue:SHOP-7 | C1 | met |" in md
        assert "| AC2 | The UI shows the limit message | pr_description | C2 | not met |" in md
        assert "[screenshot](evidence/c2.png)" in md and "banner missing" in md

    def test_headline_states_readiness_and_reason(self):
        assert "> **NOT READY**: 1 case(s) failed" in self.md([rec("C1", "fail"), rec("C2", "pass")])
        assert "> **BLOCKED**: no planned case executed" in self.md([])

    def test_axes_are_separate(self):
        md = self.md([rec("C1", "pass"), rec("C2", "pass")],
                     findings={"scope_creep": [{"behavior": "CSV export", "file": "api/export.py", "line": 12, "note": "not in any AC"}],
                               "quality": [{"severity": "low", "title": "dead code", "file": "a.py", "line": 3}]})
        assert "**Spec compliance:** 2 met" in md and "**Scope creep:** 1 behavior" in md and "api/export.py:12" in md
        assert "**Quality notes:** 1" in md

    def test_unrun_cases_listed_and_not_counted_as_passed(self):
        md = self.md([rec("C1", "pass"), rec("C2", "blocked", "playwright missing", infra=True)])
        assert "not counted as passed" in md and "C2: blocked (infrastructure)" in md and "Pass rate (executed only) | 100%" in md

    def test_delta_section(self):
        delta = dl.compare([rec("C1", "pass")], [rec("C1", "fail")])
        delta["new_log_errors"] = [{"count": 3, "sample": "ERROR boom | pipe"}]
        md = self.md([rec("C1", "fail"), rec("C2", "pass")], delta=delta, mode="regression", baseline_url="http://base")
        assert "| C1 | pass | fail | REGRESSION |" in md and "ERROR boom \\| pipe" in md and "Baseline | http://base" in md

    def test_secrets_never_reach_the_report(self):
        md = self.md([rec("C1", "fail", "Authorization: Bearer abcdefghijklmnop123", observed="token=supersecretvalue99"), rec("C2", "pass")])
        assert "abcdefghijklmnop123" not in md and "supersecretvalue99" not in md

    def test_cases_without_results_show_not_run(self):
        assert "| C2 login | " not in self.md([rec("C1", "pass")]) and "C2 limit banner | UI | AC2 | not-run" in self.md([rec("C1", "pass")])


class TestOutputs:
    def test_status_line_mapping(self):
        ready = rp.status_line(assess([rec("C1", "pass"), rec("C2", "pass")]), None)
        assert (ready["status"], ready["signal"]) == ("DONE", "DONE")
        notready = rp.status_line(assess([rec("C1", "fail"), rec("C2", "pass")]), None)
        assert (notready["status"], notready["readiness"]) == ("DONE", "Not ready")
        blocked = rp.status_line(assess([]), None)
        assert (blocked["status"], blocked["signal"]) == ("ERROR", "BLOCKED") and blocked["executed"] == 0 and blocked["failures"] == 0

    def test_cli_end_to_end(self, tmp_path, capsys):
        (tmp_path / "plan.json").write_text(json.dumps(PLAN))
        (tmp_path / "cand.jsonl").write_text("\n".join(json.dumps(r) for r in [rec("C1", "pass"), rec("C2", "pass")]))
        (tmp_path / "pr.json").write_text(json.dumps({"pr": {"title": "Add cart limit", "url": "https://github.com/a/b/pull/1"}, "ac_source": "issue:SHOP-7"}))
        out = tmp_path / "reports"
        assert rp.main(["--plan", str(tmp_path / "plan.json"), "--candidate", str(tmp_path / "cand.jsonl"), "--pr", str(tmp_path / "pr.json"),
                        "--target-url", "http://x", "--out-dir", str(out)]) == 0
        assert (out / "report.md").read_text().startswith("# QA report: Add cart limit")
        assert "<table" in (out / "report.html").read_text().lower()
        result = json.loads((out / "result.json").read_text())
        assert result["readiness"] == "Ready" and result["ac_status"]["AC1"] == "met"
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "DONE"

    def test_cli_missing_plan(self, tmp_path, capsys):
        assert rp.main(["--plan", str(tmp_path / "none.json"), "--candidate", str(tmp_path / "c.jsonl")]) == 2


class TestEvidenceCollection:
    def test_evidence_is_copied_beside_the_report_redacted_and_linked(self, tmp_path):
        shot = tmp_path / "shots" / "c2.png"
        shot.parent.mkdir()
        shot.write_bytes(b"\x89PNG")
        ev = tmp_path / "evidence" / "C1-1.txt"
        ev.parent.mkdir()
        ev.write_text("curl -X GET /x\nstatus: 500\n---\nAuthorization: Bearer abcdefghijklmnop123 password=hunter2hunter2")
        records = [rec("C1", "fail", "got 500", evidence=str(ev)), rec("C2", "fail", "banner missing", screenshot=str(shot))]
        out = tmp_path / "reports"
        rp.write_outputs(PLAN, records, None, [], {}, {"title": "t"}, out, [tmp_path])
        md = (out / "report.md").read_text()
        assert "[evidence](evidence/C1-1.txt)" in md and "[screenshot](evidence/c2.png)" in md
        assert (out / "evidence" / "c2.png").read_bytes() == b"\x89PNG"
        copied = (out / "evidence" / "C1-1.txt").read_text()
        assert "abcdefghijklmnop123" not in copied and "hunter2hunter2" not in copied and "status: 500" in copied
        assert str(tmp_path) not in md  # links are relative: they work from the uploaded reports/ folder alone

    def test_missing_evidence_files_are_skipped_not_fatal(self, tmp_path):
        records = [rec("C1", "pass", evidence="/nonexistent/x.txt"), rec("C2", "pass", screenshot="/nonexistent/y.png")]
        out = tmp_path / "reports"
        rp.write_outputs(PLAN, records, None, [], {}, {}, out, [tmp_path])
        assert "](evidence/" not in (out / "report.md").read_text() and not (out / "evidence").exists()

    def test_delta_table_links_both_sides(self, tmp_path):
        b, c = tmp_path / "b.png", tmp_path / "c.png"
        b.write_bytes(b"b"); c.write_bytes(b"c")
        base = [rec("C1", "pass", screenshot=str(b)), rec("C2", "pass")]
        cand = [rec("C1", "fail", screenshot=str(c)), rec("C2", "pass")]
        delta = dl.compare(base, cand)
        out = tmp_path / "reports"
        rp.write_outputs(PLAN, cand, delta, [], {}, {"mode": "regression"}, out, [tmp_path])
        md = (out / "report.md").read_text()
        assert "[screenshot](evidence/b.png) / [screenshot](evidence/c.png)" in md

    def test_same_basename_from_two_sides_does_not_collide(self, tmp_path):
        (tmp_path / "base").mkdir(); (tmp_path / "cand").mkdir()
        (tmp_path / "base" / "s.png").write_bytes(b"1"); (tmp_path / "cand" / "s.png").write_bytes(b"2")
        from scripts.common.report_bundle import collect_evidence
        links = collect_evidence(tmp_path / "out", [str(tmp_path / "base" / "s.png"), str(tmp_path / "cand" / "s.png")], [tmp_path])
        assert sorted(links.values()) == ["evidence/s-2.png", "evidence/s.png"]
        assert (tmp_path / "out" / "evidence" / "s.png").read_bytes() == b"1" and (tmp_path / "out" / "evidence" / "s-2.png").read_bytes() == b"2"


class TestSafetyAndStrictness:
    def test_regression_mode_without_a_delta_is_blocked(self):
        a = assess([rec("C1", "pass"), rec("C2", "pass")], delta=None)
        a_reg = rp.assess(PLAN, [rec("C1", "pass"), rec("C2", "pass")], None, [], {}, mode="regression")
        assert a["readiness"] == "Ready" and a_reg["readiness"] == "Blocked" and "needs a delta" in a_reg["blockers"][0]

    def test_invalid_or_empty_plan_is_blocked_even_with_passing_records(self):
        a = rp.assess({}, [rec("X", "pass")], None, [], {})
        assert a["readiness"] == "Blocked" and any("plan is invalid" in b for b in a["blockers"])

    def test_generated_smoke_checks_never_make_a_run_executed(self):
        a = assess([rec("SMOKE1", "pass")])
        assert a["readiness"] == "Blocked" and a["summary"]["executed"] == 0 and "SMOKE1" in a["extra"]
        md = rp.render_markdown(PLAN, a, {}, None, {})
        assert "Additional checks (not part of the plan" in md and "| SMOKE1 | pass |" in md

    def test_planned_case_without_any_record_counts_as_not_run_in_the_denominator(self):
        a = assess([rec("C1", "pass")])
        assert a["summary"]["total"] == 2 and a["summary"]["counts"]["not-run"] == 1 and a["readiness"] == "Not ready"

    def test_untrusted_text_cannot_inject_links_tags_or_columns(self):
        evil = 'x [click](javascript:alert(1)) <img src=x onerror=alert(1)> `code` | extra'
        plan = {"criteria": [{"id": "AC1", "text": evil, "source": "pr_description"}],
                "cases": [{"id": "C1", "title": evil, "ac": ["AC1"], "type": "API", "expected": "x"}]}
        a = rp.assess(plan, [rec("C1", "fail", evil)], None, [], {"scope_creep": [{"behavior": evil, "file": evil, "line": 1, "note": evil}]})
        md = rp.render_markdown(plan, a, {"title": evil}, None, {"scope_creep": [{"behavior": evil, "file": evil, "line": 1, "note": evil}]})
        assert "](javascript" not in md.replace("\\]", "") and "<img" not in md.replace("\\<", "")
        from scripts.common.report_renderer import render_simple_html
        html = render_simple_html("t", md)
        assert "<img" not in html and "href=\"javascript" not in html and "onerror=alert(1)>" not in html

    def test_outcome_cells_cannot_be_forged_through_ids_or_delta_fields(self):
        delta = {"status": "CLEAN|x", "reason": "r", "cases": [{"case": "C1|pass", "baseline": "pass", "candidate": "fail", "delta": "REGRESSION|CLEAN"}]}
        md = rp.render_markdown(PLAN, assess([rec("C1", "pass"), rec("C2", "pass")]), {}, delta, {})
        assert "C1\\|pass" in md and "REGRESSION\\|CLEAN" in md

    def test_evidence_outside_the_allowed_root_is_not_copied(self, tmp_path):
        outside = tmp_path.parent / f"{tmp_path.name}-secret.txt"
        outside.write_text("password=hunter2hunter2")
        inside = tmp_path / "ok.txt"
        inside.write_text("fine")
        links = collect_evidence_for(tmp_path / "out", [str(outside), str(inside), "/etc/hosts"], [tmp_path])
        assert list(links) == [str(inside)]

    def test_evidence_is_redacted_whatever_its_suffix_and_binaries_are_skipped(self, tmp_path):
        for name, body in {"log": b"token=abc123def456", "c.yaml": b"password: hunter2hunter2", ".env": b"SECRET_KEY=zzzzzzzz", "t.har": b'{"Authorization":"Bearer abcdefghijklmnop123"}'}.items():
            (tmp_path / name).write_bytes(body)
        (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02password=hunter2hunter2")
        links = collect_evidence_for(tmp_path / "out", [str(tmp_path / n) for n in ("log", "c.yaml", ".env", "t.har", "blob.bin")], [tmp_path])
        assert len(links) == 4 and str(tmp_path / "blob.bin") not in links
        copied = "".join(p.read_text() for p in (tmp_path / "out" / "evidence").iterdir())
        assert "abc123def456" not in copied and "hunter2hunter2" not in copied and "zzzzzzzz" not in copied and "abcdefghijklmnop123" not in copied

    def test_result_json_is_redacted(self, tmp_path):
        out = tmp_path / "reports"
        rp.write_outputs(PLAN, [rec("C1", "fail", "password=hunter2hunter2"), rec("C2", "pass")], None, [], {}, {"token": "tok_abcdef123456"}, out, [tmp_path])
        blob = (out / "result.json").read_text() + (out / "report.md").read_text()
        assert "hunter2hunter2" not in blob and "tok_abcdef123456" not in blob

    def test_cli_rejects_missing_explicit_inputs(self, tmp_path, capsys):
        (tmp_path / "plan.json").write_text(json.dumps(PLAN))
        (tmp_path / "cand.jsonl").write_text(json.dumps(rec("C1", "pass")))
        base = ["--plan", str(tmp_path / "plan.json"), "--candidate", str(tmp_path / "cand.jsonl"), "--out-dir", str(tmp_path / "r")]
        assert rp.main([*base, "--delta", str(tmp_path / "typo.json")]) == 2
        assert rp.main([*base, "--findings", str(tmp_path / "typo.json")]) == 2
        assert rp.main([*base, "--pr", str(tmp_path / "typo.json")]) == 2
        assert "cannot read" in capsys.readouterr().out

    def test_cli_regression_mode_without_delta_is_blocked_and_exits_nonzero_status(self, tmp_path, capsys):
        (tmp_path / "plan.json").write_text(json.dumps(PLAN))
        (tmp_path / "cand.jsonl").write_text("\n".join(json.dumps(rec(c, "pass")) for c in ("C1", "C2")))
        assert rp.main(["--plan", str(tmp_path / "plan.json"), "--candidate", str(tmp_path / "cand.jsonl"), "--mode", "regression", "--out-dir", str(tmp_path / "r")]) == 0
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "ERROR"

    def test_stale_env_failure_file_that_is_absent_is_ignored(self, tmp_path):
        (tmp_path / "plan.json").write_text(json.dumps(PLAN))
        (tmp_path / "cand.jsonl").write_text("\n".join(json.dumps(rec(c, "pass")) for c in ("C1", "C2")))
        assert rp.main(["--plan", str(tmp_path / "plan.json"), "--candidate", str(tmp_path / "cand.jsonl"), "--env-failure", str(tmp_path / "none.json"),
                        "--out-dir", str(tmp_path / "r")]) == 0


class TestNothingSilentlyMissing:
    def test_an_automatable_case_with_no_recorded_result_is_a_problem_not_a_concern(self):
        a = assess([rec("C1", "pass")])  # C2 (UI) never reported anything
        assert a["readiness"] == "Not ready" and any("C2" in p and "no result" in p for p in a["problems"])

    def test_an_explicit_skip_is_a_concern_not_a_pass(self):
        a = assess([rec("C1", "pass"), rec("C2", "not-run", "skipped on purpose")])
        assert a["readiness"] == "Ready with concerns" and a["ac_status"]["AC2"] == "unverifiable"

    def test_manual_cases_without_results_are_not_problems(self):
        plan = {**PLAN, "cases": [*PLAN["cases"], {"id": "C3", "title": "eyeball", "type": "MANUAL", "ac": ["AC1"], "expected": "looks right"}]}
        a = assess([rec("C1", "pass"), rec("C2", "pass")], plan=plan)
        assert not any("C3" in p for p in a["problems"])

    def test_ui_change_without_a_ui_case_blocks_the_report(self):
        plan = {**PLAN, "cases": [PLAN["cases"][0]]}
        a = rp.assess(plan, [rec("C1", "pass")], None, [], {}, changed_files=["web/src/Cart.tsx"])
        assert a["readiness"] == "Blocked" and any("UI" in b for b in a["blockers"])

    def test_ansi_codes_from_browser_errors_never_reach_the_report(self):
        a = assess([rec("C1", "pass"), rec("C2", "fail", "\x1b[31mexpected visible\x1b[39m")])
        md = rp.render_markdown(PLAN, a, {"mode": "verify"}, None, {})
        assert "\x1b" not in md and "expected visible" in md
