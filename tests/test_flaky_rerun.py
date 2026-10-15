"""Tests for scripts/flaky/rerun.py. Real subprocesses for run_once; injected run_fn for the algorithm."""

from __future__ import annotations

import json
import sys
import textwrap
import time

import pytest

from scripts.flaky import rerun as rr


def it(index, cls=None, phase="full", fp="boom", out=""):
    return rr.Iteration(index, phase, 0 if cls is None else 1, 0.01, cls == "timeout", out, cls,
                        None if cls is None else fp)


SEQ_SCRIPT = """
import os, sys
tokens = sys.argv[1].split(",")
tok = tokens[min(int(os.environ["FLAKY_RUN_INDEX"]), len(tokens) - 1)]
kind, _, detail = tok.partition(":")
if kind == "p":
    print("1 passed")
elif kind == "f":
    print("FAILED t.py::test_x - AssertionError:", detail or "boom"); sys.exit(1)
elif kind == "e":
    print("ModuleNotFoundError: No module named 'x'"); sys.exit(1)
elif kind == "b":
    print("SyntaxError: invalid syntax"); sys.exit(1)
"""


def seq(tmp_path, tokens: str, extra: tuple[str, ...] = ()) -> list[str]:
    """A real command whose pass/fail pattern is fixed per iteration index: p=pass f[:why]=fail e=env b=build."""
    script = tmp_path / "seq.py"
    if not script.exists():
        script.write_text(SEQ_SCRIPT)
    return [sys.executable, str(script), tokens, *extra]


def run(tmp_path, tokens, **kw):
    return rr.smart_rerun(seq(tmp_path, tokens), cwd=str(tmp_path), **kw)


def phases(rs):
    return [i.phase for i in rs.iterations]


class TestClassifyFailure:
    def test_pass(self):
        assert rr.classify_failure("3 passed in 0.1s", 0) == (None, "")

    def test_test_failure(self):
        assert rr.classify_failure("AssertionError: 1 != 2", 1)[0] == "test"

    def test_timeout(self):
        assert rr.classify_failure("", -1, timed_out=True)[0] == "timeout"

    @pytest.mark.parametrize("out,code", [
        ("bash: npx: command not found", 127), ("Error: Cannot find module 'jest'", 1),
        ("ModuleNotFoundError: No module named 'flask'", 1), ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock", 1),
        ("npm ERR! code E401", 1),
    ])
    def test_env_failures(self, out, code):
        assert rr.classify_failure(out, code)[0] == "env"

    @pytest.mark.parametrize("out", ["collected 0 items\n\n=== no tests ran in 0.01s ===", "?   \tpkg\t[no test files]",
                                     "Tests run: 0, Failures: 0", "Ran 0 tests in 0.000s", "0 passing (1ms)"])
    def test_zero_tests_with_green_exit_is_not_a_pass(self, out):
        cls, note = rr.classify_failure(out, 0)
        assert cls == "env" and "zero tests" in note

    @pytest.mark.parametrize("out", [
        "ok  \tgithub.com/acme/shop/pkg\t0.002s [no tests to run]",                       # go: exit 0, nothing ran
        "Test Suites: 1 skipped, 0 of 1 total\nTests:       3 skipped, 3 total",           # jest: everything skipped
        "No test files found, exiting with code 1",                                       # vitest
        "  0 passing (2ms)\n  3 pending",                                                 # mocha
        "running 0 tests\n\ntest result: ok. 0 passed; 0 failed",                         # cargo
    ])
    def test_green_exit_with_nothing_executed_is_never_a_pass(self, out):
        assert rr.classify_failure(out, 0)[0] == "env"

    @pytest.mark.parametrize("out", [
        "ok  \tgithub.com/acme/shop/a\t0.4s\n?   \tgithub.com/acme/shop/b\t[no test files]\nok  \tgithub.com/acme/shop/c\t0.1s [no tests to run]",  # go ./...
        "===== 5 passed in 0.2s =====\nlog: no tests ran for plugin x",                                                                       # pytest log noise
        "[INFO] Tests run: 0, Failures: 0\n[INFO] Tests run: 12, Failures: 0, Errors: 0",                                                    # maven multi-class
        "running 0 tests\ntest result: ok. 0 passed\nrunning 3 tests\ntest result: ok. 3 passed; 0 failed",                                # cargo multi-crate
        "ok  \tgithub.com/acme/shop/pkg\t0.502s",
        "Tests:       2 failed, 3 passed, 5 total",
        "  12 passing (1s)",
        "test result: ok. 4 passed; 0 failed",
    ])
    def test_genuine_passes_stay_passes(self, out):
        assert rr.classify_failure(out, 0) == (None, "")

    def test_exit_126_is_an_environment_problem(self):
        assert rr.classify_failure("cannot execute x: Permission denied", 126)[0] == "env"

    def test_ten_passing_is_not_mistaken_for_zero(self):
        assert rr.classify_failure("10 passing (2s)", 0) == (None, "")

    def test_zero_tests_nonzero_exit_pytest_code5(self):
        assert rr.classify_failure("collected 0 items\nno tests ran", 5)[0] == "env"

    @pytest.mark.parametrize("out", ["src/a.ts(3,1): error TS2304: Cannot find name 'x'", "SyntaxError: Unexpected token",
                                     "error[E0425]: cannot find value `y`", "COMPILATION ERROR : "])
    def test_build_failures(self, out):
        assert rr.classify_failure(out, 1)[0] == "build"


class TestFingerprint:
    def test_numbers_paths_hex_and_strings_normalized(self):
        a = rr.fingerprint("AssertionError: expected 5 got 7 at /tmp/abc/x/file.py id 0xdeadbeef 'foo'")
        b = rr.fingerprint("AssertionError: expected 12 got 3 at /var/zzz/q/other.py id 0x1234abcd 'bar'")
        assert a == b

    def test_different_errors_differ(self):
        assert rr.fingerprint("TimeoutError: exceeded") != rr.fingerprint("AssertionError: nope")

    def test_none_when_no_failure_line(self):
        assert rr.fingerprint("all good") is None


class TestExtractFailingTitle:
    @pytest.mark.parametrize("out,title", [
        ("FAILED tests/test_a.py::TestCart::test_limit - AssertionError", "TestCart::test_limit"),
        ("  ● Cart › rejects the 11th item", "rejects the 11th item"),
        ("--- FAIL: TestRetry (0.01s)", "TestRetry"),
        ("rspec ./spec/a_spec.rb:12 # Cart adds item", "Cart adds item"),
    ])
    def test_formats(self, out, title):
        assert rr.extract_failing_title(out) == title

    def test_none(self):
        assert rr.extract_failing_title("ok") is None

    def test_numbered_log_lines_are_never_mistaken_for_test_titles(self):
        assert rr.extract_failing_title("  1) Cart adds an item:\n  2) something else") is None  # mocha's listing is ambiguous


class TestVerdictHonesty:
    def test_a_single_failing_run_is_inconclusive_not_deterministic(self, tmp_path):
        v = rr.verdict(run(tmp_path, "f", runs=1), 1)
        assert v["verdict"] == "INCONCLUSIVE" and "too few completed runs" in v["statement"]
        assert rr.verdict(run(tmp_path, "f", runs=4, early_stop=False), 4)["verdict"] == "INCONCLUSIVE"
        assert rr.verdict(run(tmp_path, "f", runs=5, early_stop=False), 5)["verdict"] == "DETERMINISTIC_FAIL"

    def test_early_stop_on_a_mix_reports_no_rate(self, tmp_path):
        v = rr.verdict(run(tmp_path, "p,p,f", runs=20), 20)
        assert v["verdict"] == "FLAKY" and v["failure_rate"] is None and v["wilson95"] is None
        assert "not estimated" in v["statement"] and "1/3" in v["statement"]

    def test_full_budget_run_reports_rate_and_interval(self, tmp_path):
        v = rr.verdict(run(tmp_path, "p,f,p,p,p,p,p,p,p,p", runs=10, early_stop=False), 10)
        assert v["failure_rate"] == 0.1 and v["wilson95"][0] < 0.1 < v["wilson95"][1]

    def test_parallel_runs_carry_a_warning(self, tmp_path):
        v = rr.verdict(run(tmp_path, "p", runs=10, parallel=3), 10)
        assert v["parallel"] == 3 and "3-way parallel" in v["statement"]
        assert "parallel" not in rr.verdict(run(tmp_path, "p", runs=10), 10)["statement"]


class TestWilsonAndStatement:
    def test_zero_failures_upper_bound(self):
        lo, hi = rr.wilson(0, 20)
        assert lo == 0.0 and 0.15 < hi < 0.17  # ~16.1%

    def test_all_failures(self):
        lo, hi = rr.wilson(20, 20)
        assert hi == 1.0 and lo > 0.8

    def test_no_runs(self):
        assert rr.wilson(0, 0) == (0.0, 1.0)


class TestStopReason:
    def test_env_first_iteration(self):
        assert rr.stop_reason([it(0, "env")]) == "environment_failure"

    def test_mixed(self):
        assert rr.stop_reason([it(0), it(1, "test")]) == "mixed_pass_fail"

    def test_consecutive_identical_failures(self):
        assert rr.stop_reason([it(i, "test") for i in range(5)]) == "consecutive_failures"
        assert rr.stop_reason([it(i, "test", fp=f"e{i}") for i in range(5)]) == ""

    def test_all_pass_keeps_going(self):
        assert rr.stop_reason([it(i) for i in range(10)]) == ""


class TestSmartRerun:
    @staticmethod
    def iso(tmp_path):
        return lambda title: seq(tmp_path, "p", ("--isolated", title)) if title else None

    def test_flaky_detected_and_stops_at_mix(self, tmp_path):
        rs = run(tmp_path, "p,p,f", runs=20)
        assert rs.stop_reason == "mixed_pass_fail" and len(rs.iterations) == 3
        assert rr.verdict(rs, 20)["verdict"] == "FLAKY"

    def test_cannot_reproduce_runs_full_budget(self, tmp_path):
        rs = run(tmp_path, "p", runs=10)
        v = rr.verdict(rs, 10)
        assert v["verdict"] == "CANNOT_REPRODUCE" and v["executed"] == 10 and rs.stop_reason == "completed"
        assert "0/10" in v["statement"] and "upper bound" in v["statement"]

    def test_too_few_runs_is_called_inconclusive(self, tmp_path):
        v = rr.verdict(run(tmp_path, "p", runs=4), 4)
        assert v["verdict"] == "CANNOT_REPRODUCE" and "inconclusive" in v["statement"]
        v = rr.verdict(run(tmp_path, "p", runs=10), 10)
        assert "inconclusive" not in v["statement"]

    def test_deterministic_fail_stops_after_threshold(self, tmp_path):
        rs = run(tmp_path, "f", runs=20)
        v = rr.verdict(rs, 20)
        assert v["verdict"] == "DETERMINISTIC_FAIL" and v["executed"] == 5 and rs.stop_reason == "consecutive_failures"

    def test_env_failure_on_first_run_stops_immediately(self, tmp_path):
        rs = run(tmp_path, "e", runs=20)
        v = rr.verdict(rs, 20)
        assert v["verdict"] == "ENV_BLOCKED" and len(rs.iterations) == 1 and v["completed"] == 0

    def test_env_failures_are_excluded_from_statistics(self, tmp_path):
        v = rr.verdict(run(tmp_path, "p,e,p,f", runs=4, early_stop=False), 4)
        assert (v["passes"], v["failures"], v["env_failures"], v["completed"]) == (2, 1, 1, 3)

    def test_two_phase_uses_isolated_command_after_failure(self, tmp_path):
        rs = rr.smart_rerun(seq(tmp_path, "p,f,p"), cwd=str(tmp_path), runs=10, isolate=self.iso(tmp_path), early_stop=False)
        assert phases(rs)[:2] == ["full", "full"] and set(phases(rs)[2:]) == {"isolated"} and len(rs.iterations) == 10
        assert rs.argv_isolated[-2:] == ["--isolated", "test_x"]  # the failing title came from the phase-1 output

    def test_two_phase_stays_on_full_file_when_phase1_passes(self, tmp_path):
        rs = rr.smart_rerun(seq(tmp_path, "p"), cwd=str(tmp_path), runs=10, isolate=self.iso(tmp_path))
        assert set(phases(rs)) == {"full"} and len(rs.iterations) == 10 and rs.argv_isolated is None

    def test_phase1_size_is_twenty_percent(self, tmp_path):
        rs = rr.smart_rerun(seq(tmp_path, "f,p"), cwd=str(tmp_path), runs=20, isolate=self.iso(tmp_path), early_stop=False)
        assert phases(rs).count("full") == 4 and phases(rs).count("isolated") == 16

    def test_parallel_batches_keep_unique_indexes_and_order(self, tmp_path):
        rs = run(tmp_path, "p,p,f,p,p,p,p,p", runs=8, parallel=4, early_stop=False)
        assert [i.index for i in rs.iterations] == list(range(8))
        assert [i.failure_class for i in rs.iterations] == [None, None, "test", None, None, None, None, None]

    def test_build_failure_flagged(self, tmp_path):
        v = rr.verdict(run(tmp_path, "b", runs=6), 6)
        assert v["verdict"] == "DETERMINISTIC_FAIL" and v["build_failure"] is True

    def test_distinct_failures_do_not_trigger_the_consecutive_stop(self, tmp_path):
        rs = run(tmp_path, "f:alpha,f:beta,f:gamma,f:delta,f:epsilon,f:zeta", runs=6)
        assert rs.stop_reason == "completed" and len(rs.iterations) == 6


class TestRunOnce:
    def _script(self, tmp_path, body):
        p = tmp_path / "t.py"
        p.write_text(textwrap.dedent(body))
        return [sys.executable, str(p)]

    def test_pass_and_fail_and_missing_binary(self, tmp_path):
        ok = rr.run_once(self._script(tmp_path, "print('1 passed')"), str(tmp_path), None, 30)
        assert ok.failure_class is None and ok.exit_code == 0
        bad = rr.run_once(self._script(tmp_path, "import sys; print('AssertionError: nope'); sys.exit(1)"), str(tmp_path), None, 30)
        assert bad.failure_class == "test" and "nope" in bad.output_tail and bad.fingerprint
        missing = rr.run_once(["definitely-not-a-binary-xyz"], str(tmp_path), None, 30)
        assert missing.failure_class == "env" and missing.exit_code == 127

    def test_run_index_and_env_passed_to_child(self, tmp_path):
        argv = self._script(tmp_path, "import os; print(os.environ['FLAKY_RUN_INDEX'], os.environ['MY_VAR'], '1 passed')")
        res = rr.run_once(argv, str(tmp_path), {"MY_VAR": "v"}, 30, index=7)
        assert "7 v" in res.output_tail

    def test_timeout_is_classified_not_raised(self, tmp_path):
        res = rr.run_once(self._script(tmp_path, "import threading; threading.Event().wait()"), str(tmp_path), None, 1)
        assert res.timed_out and res.failure_class == "timeout"

    def test_timeout_kills_the_whole_process_group(self, tmp_path):
        """A grandchild that inherits the output pipe must not keep the run (or the caller) alive."""
        import os
        import threading
        marker = tmp_path / "grandchild.pid"
        child = ("import subprocess, sys, pathlib, threading;"
                 "p = subprocess.Popen([sys.executable, '-c', 'import threading; threading.Event().wait()']);"
                 f"pathlib.Path({str(marker)!r}).write_text(str(p.pid)); threading.Event().wait()")
        res = rr.run_once([sys.executable, "-c", child], str(tmp_path), None, 2)
        assert res.timed_out and res.failure_class == "timeout" and res.duration_s < 20  # returned; did not hang on the pipe
        pid = int(marker.read_text())

        def gone() -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False

        deadline = time.monotonic() + 10  # condition wait: SIGKILL delivery/reaping is asynchronous
        while not gone() and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        assert gone(), "grandchild survived the timeout"

    def test_run_capture_missing_binary_and_merged_streams(self, tmp_path):
        assert rr.run_capture(["no-such-binary-xyz"], str(tmp_path), None, 5) == ("command not found: no-such-binary-xyz", 127, False)
        out, code, timed_out = rr.run_capture([sys.executable, "-c", "import sys; print('a'); print('b', file=sys.stderr)"], str(tmp_path), None, 30)
        assert (code, timed_out) == (0, False) and "a" in out and "b" in out

    def test_missing_working_directory_is_reported_as_such(self, tmp_path):
        out, code, timed = rr.run_capture(["true"], str(tmp_path / "nope"), None, 5)
        assert code == 126 and "working directory not found" in out and not timed
        assert rr.run_capture([], str(tmp_path), None, 5)[1] == 126

    def test_platform_credentials_are_not_passed_to_the_code_under_test(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "platform-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "platform-secret")
        out, code, _ = rr.run_capture([sys.executable, "-c", "import os; print(os.environ.get('GH_TOKEN'), os.environ.get('ANTHROPIC_API_KEY'), os.environ.get('KEEP'))"],
                                      str(tmp_path), {"KEEP": "yes"}, 30)
        assert code == 0 and out.strip() == "None None yes"

    def test_background_processes_started_by_a_run_do_not_outlive_it(self, tmp_path):
        import os
        import threading
        marker = tmp_path / "bg.pid"
        script = ("import subprocess, sys, pathlib;"
                  "p = subprocess.Popen([sys.executable, '-c', 'import threading; threading.Event().wait()']);"
                  f"pathlib.Path({str(marker)!r}).write_text(str(p.pid))")
        _, code, _ = rr.run_capture([sys.executable, "-c", script], str(tmp_path), None, 30)
        assert code == 0
        pid = int(marker.read_text())

        def gone():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False

        deadline = time.monotonic() + 10
        while not gone() and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        assert gone(), "a server left running by one run would hold its port in the next"

    def test_huge_output_is_clipped_without_loading_it_all(self, tmp_path):
        out, code, _ = rr.run_capture([sys.executable, "-c", "print('x' * 6_000_000); print('THE-END')"], str(tmp_path), None, 60, max_bytes=100_000)
        assert code == 0 and len(out) < 150_000 and "bytes omitted" in out and out.rstrip().endswith("THE-END")

    def test_long_output_is_clipped(self, tmp_path):
        res = rr.run_once(self._script(tmp_path, "print('x' * 20000); print('1 passed')"), str(tmp_path), None, 30)
        assert len(res.output_tail) < 5000 and "omitted" in res.output_tail and res.output_tail.endswith("1 passed\n")


class TestCli:
    def test_end_to_end_flaky_counter_script(self, tmp_path, capsys):
        counter = tmp_path / "n"
        script = tmp_path / "flaky.py"
        script.write_text(textwrap.dedent(f"""
            import pathlib, sys
            c = pathlib.Path({str(counter)!r}); n = int(c.read_text() or 0) + 1 if c.exists() else 1
            c.write_text(str(n))
            if n % 3 == 0:
                print("FAILED t.py::test_x - AssertionError: expected 1 got", n); sys.exit(1)
            print("1 passed")
        """))
        out = tmp_path / "runs.json"
        rc = rr.main(["--cwd", str(tmp_path), "--cmd", f"{sys.executable} {script}", "--runs", "9", "--out", str(out)])
        data = json.loads(out.read_text())
        assert rc == 0 and data["summary"]["verdict"] == "FLAKY" and data["summary"]["stop_reason"] == "mixed_pass_fail"
        assert "FLAKY_VERDICT::FLAKY" in capsys.readouterr().out

    @pytest.mark.parametrize("args", [["--cmd", "true", "--env", "NOEQUALS"], ["--cmd", ""], ["--cmd", "   "]])
    def test_rejects_bad_env_and_empty_command(self, args, capsys):
        assert rr.main(args) == 2 and capsys.readouterr().err

    def test_rejects_zero_runs(self):
        assert rr.main(["--cmd", "true", "--runs", "0"]) == 2

    def test_isolate_template_replaces_test_placeholder(self):
        build = rr._isolate_from_template(["run", "-t", "{test}"])
        assert build("adds item") == ["run", "-t", "adds item"] and build(None) is None
