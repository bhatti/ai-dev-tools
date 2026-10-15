"""Tests for scripts/flaky/report.py and scripts/common/redact.py."""

from __future__ import annotations

import json

import pytest

from scripts.common.redact import redact
from scripts.flaky import report as rp
from scripts.flaky import rerun as rr


def make_runs(seq):
    its = []
    for i, r in enumerate(seq):
        cls = {"p": None, "f": "test", "e": "env"}[r]
        out = {"p": "1 passed", "f": "AssertionError: expected 1 got 2 token=abc123SECRETvalue", "e": "command not found"}[r]
        its.append(rr.Iteration(i, "full", 0 if cls is None else 1, 0.1, False, out, cls, rr.fingerprint(out) if cls else None))
    rs = rr.RunSet(iterations=its, stop_reason="completed", argv_full=["run", "t"])
    return {"summary": rr.verdict(rs, len(seq)), "argv_full": ["run", "t"], "argv_isolated": None,
            "iterations": [i.__dict__ for i in its]}


class TestRedact:
    @pytest.mark.parametrize("raw,secret", [
        ("Authorization: Bearer abcdefghijklmnop123", "abcdefghijklmnop123"),
        ("export GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwx", "ghp_abcdefghijklmnopqrstuvwx"),
        ("password: 'hunter2hunter2'", "hunter2hunter2"),
        ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI"),
        ("postgres://user:s3cr3tpw@db:5432/x", "s3cr3tpw"),
        ("key AKIAABCDEFGHIJKLMNOP here", "AKIAABCDEFGHIJKLMNOP"),
        ("xoxb-1234567890-abcdefghij", "xoxb-1234567890-abcdefghij"),
        ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.abcdefghijkl", "eyJhbGciOiJIUzI1NiJ9"),
        ("Cookie: session=abcdef123456", "abcdef123456"),
    ])
    def test_secrets_removed(self, raw, secret):
        out = redact(raw)
        assert secret not in out and "redact" in out.lower()

    def test_plain_text_untouched(self):
        assert redact("AssertionError: expected 1 got 2") == "AssertionError: expected 1 got 2"
        assert redact("") == ""


class TestReport:
    def test_flaky_headline_and_stats(self):
        md = rp.render_markdown(make_runs(["p", "p", "f"]), [], {"test": "a.test.js"})
        assert "FLAKY: intermittent failures reproduced" in md and "1/3 runs failed" in md
        assert "## Not judged" in md

    def test_cannot_reproduce_never_claims_stable(self):
        md = rp.render_markdown(make_runs(["p"] * 10), [], {"test": "a.test.js"})
        assert "0/10 failures (95% upper bound" in md and "not proof the test is stable" in md
        assert "not flaky" not in md.lower().replace("not proof the test is stable", "")

    def test_env_blocked_is_error_status(self):
        runs = make_runs(["e"])
        assert rp.status_line(runs)["status"] == "ERROR" and rp.status_line(runs)["signal"] == "BLOCKED"
        assert "nothing was verified" in rp.render_markdown(runs, [], {"test": "t"})

    def test_status_mapping(self):
        assert rp.status_line(make_runs(["p", "f"]))["status"] == "DONE"
        s = rp.status_line(make_runs(["p"] * 5))
        assert (s["status"], s["signal"]) == ("DONE", "DONE_WITH_CONCERNS")
        assert rp.status_line(make_runs(["f"] * 5))["verdict"] == "DETERMINISTIC_FAIL"

    def test_secrets_redacted_in_report(self):
        md = rp.render_markdown(make_runs(["p", "f"]), [], {"test": "t"})
        assert "abc123SECRETvalue" not in md and "<redacted>" in md

    def test_findings_table_with_location(self):
        f = [{"id": "fixed_sleep", "severity": "high", "file": "t.py", "line": 5, "snippet": "time.sleep(2)",
              "evidence": "t.py:5", "recommendation": "Wait for the condition | not sleep"}]
        md = rp.render_markdown(make_runs(["p", "f"]), f, {"test": "t", "attempts": ["replace sleep with poll: still failed"]})
        assert "| high | fixed_sleep | t.py:5 | `time.sleep(2)` |" in md and "Wait for the condition \\| not sleep" in md
        assert "Hypotheses tested" in md

    def test_no_findings_message(self):
        assert "No known signature matched" in rp.render_markdown(make_runs(["p", "f"]), [], {"test": "t"})

    def test_write_outputs_and_cli(self, tmp_path, capsys):
        runs_path = tmp_path / "runs.json"
        runs_path.write_text(json.dumps(make_runs(["p", "f", "p"])))
        out = tmp_path / "reports"
        assert rp.main(["--runs", str(runs_path), "--test", "a.test.js", "--out-dir", str(out), "--rev", "abc123"]) == 0
        assert (out / "report.md").read_text().startswith("# Flaky test report")
        assert "<html" in (out / "report.html").read_text().lower() or "<table" in (out / "report.html").read_text().lower()
        result = json.loads((out / "result.json").read_text())
        assert result["verdict"] == "FLAKY" and result["meta"]["rev"] == "abc123"
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "DONE"


class TestReportRobustness:
    def test_failing_output_cannot_break_out_of_its_code_fence(self):
        runs = make_runs(["p", "f"])
        runs["iterations"][1]["output_tail"] = "boom\n```\n# Injected heading\n[click](http://evil.example)\n```"
        md = rp.render_markdown(runs, [], {"test": "t"})
        fence_line = next(l for l in md.splitlines() if l.startswith("````"))
        assert md.count(fence_line) == 2 and "# Injected heading" in md.split(fence_line)[1]  # still inside the (longer) fence

    def test_env_blocked_does_not_claim_a_pass_fail_mix_stopped_the_loop(self):
        md = rp.render_markdown(make_runs(["e"]), [], {"test": "t"})
        assert "Failure rate | n/a" in md and "pass and fail were both seen" not in md

    def test_result_json_is_redacted_including_findings(self, tmp_path):
        finding = {"id": "x", "severity": "low", "evidence": "password=hunter2hunter2", "snippet": "k = 'AKIAABCDEFGHIJKLMNOP'", "recommendation": "r"}
        out = tmp_path / "r"
        rp.write_outputs(make_runs(["p", "f"]), [finding], {"test": "t"}, out)
        blob = (out / "result.json").read_text() + (out / "report.md").read_text()
        assert "hunter2hunter2" not in blob and "AKIAABCDEFGHIJKLMNOP" not in blob

    def test_inconclusive_maps_to_done_with_concerns(self):
        s = rp.status_line(make_runs(["f"]))
        assert (s["verdict"], s["status"], s["signal"]) == ("INCONCLUSIVE", "DONE", "DONE_WITH_CONCERNS")
