"""Tests for scripts/mq/run_scoped_ci.py"""

import json
from pathlib import Path

import pytest

from scripts.mq.run_scoped_ci import (
    _build_test_command,
    _detect_concurrency,
    _detect_project_type,
    _parse_slow_tests,
    _parse_test_counts,
)


class TestDetectProjectType:
    def test_python_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]")
        assert _detect_project_type(str(tmp_path)) == "python"

    def test_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module test")
        assert _detect_project_type(str(tmp_path)) == "go"

    def test_rust_cargo(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        assert _detect_project_type(str(tmp_path)) == "rust"

    def test_node_package(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert _detect_project_type(str(tmp_path)) == "node"

    def test_java_gradle(self, tmp_path):
        (tmp_path / "build.gradle").write_text("apply plugin")
        assert _detect_project_type(str(tmp_path)) == "java"

    def test_java_maven(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project/>")
        assert _detect_project_type(str(tmp_path)) == "java"

    def test_default_python(self, tmp_path):
        assert _detect_project_type(str(tmp_path)) == "python"

    def test_ruby_gemfile(self, tmp_path):
        (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'")
        assert _detect_project_type(str(tmp_path)) == "ruby"

    def test_kotlin_gradle(self, tmp_path):
        (tmp_path / "build.gradle.kts").write_text("plugins {}")
        assert _detect_project_type(str(tmp_path)) == "kotlin"

    def test_priority_rust_over_make(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        (tmp_path / "Makefile").write_text("build:")
        assert _detect_project_type(str(tmp_path)) == "rust"

    def test_makefile_only(self, tmp_path):
        (tmp_path / "Makefile").write_text("test:\n\techo ok")
        assert _detect_project_type(str(tmp_path)) == "make"


class TestBuildTestCommand:
    """All _build_test_command calls return (cmd, extra_env) tuple."""

    def test_python(self, tmp_path):
        cmd, env = _build_test_command("python", ["tests/test_a.py"], str(tmp_path))
        assert cmd[0:3] == ["python", "-m", "pytest"]
        assert "tests/test_a.py" in cmd
        assert env == {}

    def test_python_with_junit(self, tmp_path):
        cmd, env = _build_test_command("python", ["tests/test_a.py"], str(tmp_path), "/out.xml")
        assert any("junitxml" in c for c in cmd)

    def test_go(self, tmp_path):
        cmd, env = _build_test_command("go", ["pkg/foo_test.go"], str(tmp_path), concurrency=2)
        assert cmd[0] == "go"
        assert "test" in cmd
        assert "-parallel=2" in cmd
        assert env["GOMAXPROCS"] == "2"

    def test_go_with_gotestsum(self, tmp_path):
        cmd, env = _build_test_command("go", ["pkg/foo_test.go"], str(tmp_path),
                                       junit_path="/out.xml", concurrency=2)
        assert cmd[0] == "gotestsum"
        assert "-parallel=2" in cmd

    def test_rust_crate(self, tmp_path):
        cmd, env = _build_test_command("rust", ["crates/core/tests/lib.rs"], str(tmp_path),
                                       concurrency=4)
        assert "cargo" in cmd
        assert "test" in cmd
        assert "-p" in cmd
        assert "core" in cmd
        assert "--test-threads=4" in cmd

    def test_rust_single_crate(self, tmp_path):
        cmd, env = _build_test_command("rust", ["src/lib.rs"], str(tmp_path), concurrency=2)
        assert cmd[:2] == ["cargo", "test"]
        assert "-p" not in cmd

    def test_node(self, tmp_path):
        cmd, env = _build_test_command("node", ["src/app.test.ts"], str(tmp_path), concurrency=2)
        assert "jest" in " ".join(cmd)
        assert "--maxWorkers=2" in cmd

    def test_java_gradle(self, tmp_path):
        (tmp_path / "build.gradle").write_text("")
        cmd, env = _build_test_command("java", ["src/test/java/FooTest.java"], str(tmp_path),
                                       concurrency=2)
        assert "gradlew" in cmd[0]
        assert "--max-workers=2" in cmd

    def test_java_maven(self, tmp_path):
        cmd, env = _build_test_command("java", ["src/test/java/FooTest.java"], str(tmp_path),
                                       concurrency=2)
        assert cmd[0] == "mvn"
        assert "-T2" in cmd

    def test_ruby(self, tmp_path):
        cmd, env = _build_test_command("ruby", ["spec/models/foo_spec.rb"], str(tmp_path))
        assert "rspec" in cmd
        assert "spec/models/foo_spec.rb" in cmd

    def test_kotlin(self, tmp_path):
        cmd, env = _build_test_command("kotlin", ["src/test/kotlin/FooTest.kt"], str(tmp_path),
                                       concurrency=2)
        assert "gradlew" in cmd[0]
        assert "--max-workers=2" in cmd

    def test_csharp(self, tmp_path):
        cmd, env = _build_test_command("csharp", ["src/FooTests.cs"], str(tmp_path), concurrency=2)
        assert "dotnet" in cmd
        assert env["DOTNET_PROCESSOR_COUNT"] == "2"

    def test_make(self, tmp_path):
        cmd, env = _build_test_command("make", ["tests/test_a.py"], str(tmp_path))
        assert cmd == ["make", "test"]

    def test_unknown_falls_to_pytest(self, tmp_path):
        cmd, env = _build_test_command("unknown_lang", ["test_a.py"], str(tmp_path))
        assert cmd[0:3] == ["python", "-m", "pytest"]


class TestDetectConcurrency:
    def test_returns_positive_int(self):
        n = _detect_concurrency()
        assert isinstance(n, int)
        assert n >= 1


class TestParseTestCounts:
    def test_pytest_output(self):
        stdout = "===== 5 passed, 2 failed, 1 skipped in 3.2s ====="
        passed, failed, skipped = _parse_test_counts(stdout, "")
        assert passed == 5
        assert failed == 2
        assert skipped == 1

    def test_all_pass(self):
        stdout = "===== 10 passed in 1.5s ====="
        passed, failed, skipped = _parse_test_counts(stdout, "")
        assert passed == 10
        assert failed == 0

    def test_go_output(self):
        stdout = "ok  \tgithub.com/org/repo/pkg\t0.5s\nok  \tgithub.com/org/repo/cmd\t1.2s\n"
        passed, failed, skipped = _parse_test_counts(stdout, "")
        assert passed == 2
        assert failed == 0

    def test_go_failure(self):
        stdout = "FAIL\tgithub.com/org/repo/pkg\t2.1s\n"
        passed, failed, skipped = _parse_test_counts(stdout, "")
        assert failed == 1

    def test_rust_output(self):
        stdout = "test result: ok. 12 passed; 1 failed; 3 ignored; 0 measured; 0 filtered out"
        passed, failed, skipped = _parse_test_counts(stdout, "")
        assert passed == 12
        assert failed == 1
        assert skipped == 3

    def test_jest_output(self):
        stdout = "Tests:  2 failed, 18 passed, 20 total"
        passed, failed, skipped = _parse_test_counts(stdout, "")
        assert passed == 18
        assert failed == 2

    def test_jest_all_pass(self):
        stdout = "Tests:  25 passed, 25 total"
        passed, failed, skipped = _parse_test_counts(stdout, "")
        assert passed == 25
        assert failed == 0

    def test_rspec_output(self):
        stdout = "42 examples, 3 failures, 2 pending"
        passed, failed, skipped = _parse_test_counts(stdout, "")
        assert passed == 37
        assert failed == 3
        assert skipped == 2

    def test_dotnet_output(self):
        stdout = "Passed!  - Failed: 1, Passed: 14, Skipped: 2, Total: 17"
        passed, failed, skipped = _parse_test_counts(stdout, "")
        assert passed == 14
        assert failed == 1
        assert skipped == 2

    def test_empty_output(self):
        passed, failed, skipped = _parse_test_counts("", "")
        assert passed == 0
        assert failed == 0
        assert skipped == 0


class TestParseSlowTests:
    def test_pytest_durations(self):
        stdout = (
            "= slowest 5 durations =\n"
            "2.31s call     tests/test_heavy.py::test_big_query\n"
            "1.05s call     tests/test_api.py::test_timeout\n"
            "0.45s call     tests/test_util.py::test_parse\n"
        )
        slow = _parse_slow_tests(stdout, "")
        assert len(slow) == 3
        assert slow[0]["name"] == "tests/test_heavy.py::test_big_query"
        assert slow[0]["duration_s"] == 2.31

    def test_go_timing(self):
        stdout = (
            "--- PASS: TestFoo (3.200s)\n"
            "--- PASS: TestBar (0.010s)\n"
            "--- FAIL: TestBaz (5.100s)\n"
        )
        slow = _parse_slow_tests(stdout, "")
        assert len(slow) == 3
        assert slow[0]["name"] == "TestBaz"
        assert slow[0]["duration_s"] == 5.1

    def test_top_n_limit(self):
        lines = [f"1.{i:02d}s call     tests/test_{i}.py::test_x\n" for i in range(10)]
        slow = _parse_slow_tests("".join(lines), "", top_n=3)
        assert len(slow) == 3

    def test_empty_output(self):
        assert _parse_slow_tests("", "") == []


class TestShardResultFilename:
    """Regression tests for the indexed shard result filename.

    Each fan-out child writes shard_result_{shard_id}.json so that when the report
    task downloads all fan-out artifacts into a shared workspace, files from different
    shards don't overwrite each other.  The report task globs shard_result_*.json.
    """

    def test_no_tests_writes_indexed_file(self, tmp_path, monkeypatch):
        import sys
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        monkeypatch.setenv("CODEBASE_DIR", str(tmp_path))
        # Patch the main function to use a no-tests shard
        from scripts.mq import run_scoped_ci
        shard_json = json.dumps({"shard_id": 3, "tests": []})
        with pytest.raises(SystemExit) as exc:
            run_scoped_ci.main.main(["--shard", shard_json], standalone_mode=False)
        # Either exits 0 (skipped) or runs — either way the indexed file must exist
        assert (tmp_path / "shard_result_3.json").exists(), \
            "Expected shard_result_3.json; plain shard_result.json would collide between fan-out children"
        assert not (tmp_path / "shard_result.json").exists(), \
            "shard_result.json (un-indexed) must not be written — it would be overwritten on artifact download"

    def test_indexed_file_matches_glob_pattern(self, tmp_path):
        import glob
        (tmp_path / "shard_result_0.json").write_text("{}")
        (tmp_path / "shard_result_1.json").write_text("{}")
        matches = glob.glob(str(tmp_path / "shard_result_*.json"))
        assert len(matches) == 2, "Report task glob must find all shards"
