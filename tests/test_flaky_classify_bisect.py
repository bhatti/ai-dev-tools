"""Tests for scripts/flaky/classify.py and scripts/flaky/bisect_polluter.py."""

from __future__ import annotations

import datetime
import json
import sys
import textwrap

import pytest

from scripts.flaky import bisect_polluter as bp
from scripts.flaky import classify as cl

TODAY = datetime.date(2026, 10, 15)


def ids(findings):
    return {f["id"] for f in findings}


class TestScanOutput:
    @pytest.mark.parametrize("line,fid", [
        ("Error: listen EADDRINUSE: address already in use :::3000", "port_conflict"),
        ("Timeout of 5000ms exceeded", "timeout"),
        ("Error: connect ECONNREFUSED 127.0.0.1:5432", "network_error"),
        ("TypeError: Cannot read properties of undefined (reading 'id')", "async_race"),
        ("WARNING: DATA RACE", "data_race"),
        ("ENOENT: no such file or directory, open 'x'", "filesystem_state"),
        ("FATAL ERROR: heap out of memory", "resource_pressure"),
        ("x509: certificate has expired or is not yet valid", "clock_expiry"),
    ])
    def test_signatures(self, line, fid):
        found = cl.scan_output(f"noise\n{line}\nmore")
        assert fid in ids(found) and found[0]["evidence"]

    def test_clean_output(self):
        assert cl.scan_output("3 passed") == []


class TestScanSource:
    SRC = textwrap.dedent("""\
        // time.sleep(5) in a comment is ignored
        import time, random
        items = []
        def test_a():
            time.sleep(2)
            s = random.randint(1, 9)
            srv.listen(8080)
            fetch("https://api.partner.io/v1/x")
            path = "/tmp/out.txt"
            d = "2027-01-01"
            now = datetime.now()
        """)

    def test_findings_with_file_line(self):
        found = {f["id"]: f for f in cl.scan_source("t.py", self.SRC, TODAY)}
        assert {"fixed_sleep", "unseeded_random", "fixed_port", "network_dependency", "tmp_path_shared", "time_bomb",
                "wall_clock", "shared_state"} <= set(found)
        assert found["fixed_sleep"]["line"] == 5 and found["fixed_sleep"]["evidence"] == "t.py:5"
        assert "time.sleep(2)" in found["fixed_sleep"]["snippet"]
        assert found["shared_state"]["line"] == 3

    def test_comment_lines_ignored(self):
        assert "fixed_sleep" not in ids([f for f in cl.scan_source("t.py", "# time.sleep(1)\n// setTimeout(f, 5)\n", TODAY)])

    def test_seeded_file_suppresses_random(self):
        src = "import random\nrandom.seed(42)\nx = random.randint(1, 5)\n"
        assert "unseeded_random" not in ids(cl.scan_source("t.py", src, TODAY))

    def test_past_dates_are_not_time_bombs(self):
        assert "time_bomb" not in ids(cl.scan_source("t.py", 'd = "2024-01-01"\n', TODAY))
        assert "time_bomb" in ids(cl.scan_source("t.py", 'd = "2026-12-31"\n', TODAY))

    @pytest.mark.parametrize("name,src,fid", [
        ("a.test.js", "await new Promise(r => setTimeout(r, 500));", "fixed_sleep"),
        ("a.test.js", "const t = Date.now();", "wall_clock"),
        ("a_test.go", "time.Sleep(time.Second)", "fixed_sleep"),
        ("a_test.go", 'http.ListenAndServe(":8081", nil)', "fixed_port"),
        ("A.java", "Thread.sleep(1000);", "fixed_sleep"),
        ("a.test.js", "expect(Object.keys(o)).toEqual(['a'])", "unordered_iteration"),
        ("a.test.js", "jest.retryTimes(3)", "retry_masking"),
        ("a.py", "t = threading.Thread(target=f)", "concurrency_in_test"),
    ])
    def test_multi_language(self, name, src, fid):
        assert fid in ids(cl.scan_source(name, src + "\n", TODAY))

    def test_per_id_cap(self):
        src = "\n".join("time.sleep(1)" for _ in range(20))
        assert len([f for f in cl.scan_source("t.py", src, TODAY, per_id=3) if f["id"] == "fixed_sleep"]) == 3


def _iter(phase, cls, out=""):
    return {"phase": phase, "failure_class": cls, "output_tail": out}


class TestDiagnose:
    def test_order_dependence_when_only_full_runs_fail(self):
        runs = {"iterations": [_iter("full", "test", "boom"), _iter("isolated", None), _iter("isolated", None),
                               _iter("isolated", None)]}
        found = cl.diagnose(runs, {}, TODAY)
        assert found[0]["id"] == "order_dependence" and found[0]["severity"] == "high"

    def test_no_order_dependence_if_isolated_also_fails(self):
        runs = {"iterations": [_iter("full", "test"), _iter("isolated", "test"), _iter("isolated", None)]}
        assert "order_dependence" not in ids(cl.diagnose(runs, {}, TODAY))

    def test_static_replaces_runtime_alias_and_keeps_evidence(self):
        runs = {"iterations": [_iter("full", "timeout", "Timeout of 2000ms exceeded")]}
        found = cl.diagnose(runs, {"t.js": "await new Promise(r => setTimeout(r, 3000));\n"}, TODAY)
        assert "timeout" not in ids(found)  # replaced by the static fixed_sleep hit
        sleep = next(f for f in found if f["id"] == "fixed_sleep")
        assert sleep["file"] == "t.js" and "Timeout of 2000ms" in sleep["runtime_evidence"]

    def test_runtime_kept_when_no_static_counterpart(self):
        runs = {"iterations": [_iter("full", "test", "listen EADDRINUSE :::80")]}
        assert ids(cl.diagnose(runs, {"t.js": "x = 1\n"}, TODAY)) == {"port_conflict"}

    def test_sorted_by_severity_then_static_first(self):
        runs = {"iterations": [_iter("full", "test", "ENOENT: nope")]}
        found = cl.diagnose(runs, {"t.py": "time.sleep(1)\nd = datetime.now()\n"}, TODAY)
        assert [f["severity"] for f in found] == sorted([f["severity"] for f in found], key=cl.SEVERITY_ORDER.get)

    def test_passing_iterations_output_ignored(self):
        runs = {"iterations": [_iter("full", None, "EADDRINUSE in a passing run's logs")]}
        assert cl.diagnose(runs, {}, TODAY) == []


class TestDdmin:
    def test_single_polluter_found_with_few_calls(self):
        files = [f"f{i}" for i in range(16)]
        r = bp.find_polluters(files, lambda s: "f11" in s)
        assert r["status"] == "found" and r["polluters"] == ["f11"] and r["predicate_calls"] < 16

    def test_two_cooperating_polluters(self):
        r = bp.find_polluters([f"f{i}" for i in range(8)], lambda s: "f2" in s and "f6" in s)
        assert r["status"] == "found" and r["polluters"] == ["f2", "f6"]

    def test_fails_alone_means_not_order_dependent(self):
        assert bp.find_polluters(["a", "b"], lambda s: True)["status"] == "fails_alone"

    def test_not_reproduced(self):
        assert bp.find_polluters(["a", "b"], lambda s: False)["status"] == "not_reproduced"
        assert bp.find_polluters([], lambda s: False)["status"] == "not_reproduced"

    def test_budget_exhausted_reports_best_so_far(self):
        r = bp.find_polluters([f"f{i}" for i in range(32)], lambda s: "f30" in s, max_runs=3)
        assert r["status"] == "budget_exhausted" and "f30" in r["polluters"]

    def test_order_preserved(self):
        r = bp.find_polluters(["z", "m", "a"], lambda s: "z" in s and "a" in s)
        assert r["polluters"] == ["z", "a"]


class TestCli:
    def _project(self, tmp_path):
        runner = tmp_path / "runner.py"
        runner.write_text(textwrap.dedent("""
            import sys, pathlib
            files = sys.argv[1:]
            polluted = any("POLLUTE" in pathlib.Path(f).read_text() for f in files[:-1])
            print("collected %d files" % len(files), "1 passed")
            if polluted:
                print("FAILED %s - state leaked" % files[-1]); sys.exit(1)
        """))
        for name, body in {"a.t": "ok", "b.t": "POLLUTE", "c.t": "ok", "target.t": "ok"}.items():
            (tmp_path / name).write_text(body)
        return runner

    def test_end_to_end_with_real_subprocesses(self, tmp_path, capsys):
        runner = self._project(tmp_path)
        rc = bp.main(["--cmd", f"{sys.executable} {runner} {{files}}", "--target", "target.t",
                      "--candidates", "a.t", "b.t", "c.t", "--fail-pattern", r"FAILED target\.t", "--cwd", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == 0 and json.loads(out.split("::add-task-context")[0])["polluters"] == ["b.t"]
        assert "POLLUTER_STATUS::found" in out

    def test_missing_binary_is_env_blocked(self, tmp_path, capsys):
        bp.main(["--cmd", "no-such-binary-xyz {files}", "--target", "t", "--candidates", "a", "--fail-pattern", "FAILED", "--cwd", str(tmp_path)])
        assert "env_blocked" in capsys.readouterr().out

    def test_requires_files_token(self):
        assert bp.main(["--cmd", "run", "--target", "t", "--candidates", "a", "--fail-pattern", "x"]) == 2

    def test_target_excluded_from_candidates(self, tmp_path, capsys):
        runner = self._project(tmp_path)
        bp.main(["--cmd", f"{sys.executable} {runner} {{files}}", "--target", "target.t",
                 "--candidates", "target.t", "a.t", "--fail-pattern", r"FAILED target\.t", "--cwd", str(tmp_path)])
        assert "not_reproduced" in capsys.readouterr().out


class TestBisectEdgeCases:
    def test_a_run_that_hangs_is_inconclusive_not_a_verdict(self, tmp_path, capsys):
        runner = tmp_path / "hang.py"
        runner.write_text("import threading; threading.Event().wait()")
        pred = bp.make_predicate([sys.executable, str(runner), "{files}"], "t.t", "FAILED", str(tmp_path), timeout=1)
        with pytest.raises(bp.EnvBlocked, match="timed out"):
            pred([])

    def test_unlaunchable_command_is_env_blocked_with_a_blocking_exit_code(self, tmp_path, capsys):
        script = tmp_path / "x.py"
        script.write_text("print('hi')")  # not executable: exec fails with a permission error (exit 126)
        rc = bp.main(["--cmd", f"{script} {{files}}", "--target", "t.t", "--candidates", "a.t", "--fail-pattern", "FAILED", "--cwd", str(tmp_path)])
        assert rc == bp.EXIT_BLOCKED and "env_blocked" in capsys.readouterr().out

    def test_missing_binary_exits_with_the_blocking_code(self, tmp_path):
        assert bp.main(["--cmd", "no-such-binary-xyz {files}", "--target", "t", "--candidates", "a", "--fail-pattern", "x", "--cwd", str(tmp_path)]) == bp.EXIT_BLOCKED
