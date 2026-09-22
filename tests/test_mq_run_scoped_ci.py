"""Tests for scripts/mq/run_scoped_ci.py"""

import json
from pathlib import Path

import pytest

from scripts.mq.run_scoped_ci import (
    _build_test_command,
    _detect_project_type,
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


class TestBuildTestCommand:
    def test_python(self, tmp_path):
        cmd = _build_test_command("python", ["tests/test_a.py"], str(tmp_path))
        assert cmd[0:3] == ["python", "-m", "pytest"]
        assert "tests/test_a.py" in cmd

    def test_python_with_junit(self, tmp_path):
        cmd = _build_test_command("python", ["tests/test_a.py"], str(tmp_path), "/out.xml")
        assert any("junitxml" in c for c in cmd)

    def test_go(self, tmp_path):
        cmd = _build_test_command("go", ["pkg/foo_test.go"], str(tmp_path))
        assert cmd[0] == "go"
        assert "test" in cmd

    def test_rust_crate(self, tmp_path):
        cmd = _build_test_command("rust", ["crates/core/tests/lib.rs"], str(tmp_path))
        assert "cargo" in cmd
        assert "test" in cmd

    def test_node(self, tmp_path):
        cmd = _build_test_command("node", ["src/app.test.ts"], str(tmp_path))
        assert "jest" in cmd

    def test_java_gradle(self, tmp_path):
        (tmp_path / "build.gradle").write_text("")
        cmd = _build_test_command("java", ["src/test/java/FooTest.java"], str(tmp_path))
        assert "gradlew" in cmd[0] or "gradle" in cmd[0]

    def test_ruby(self, tmp_path):
        cmd = _build_test_command("ruby", ["spec/models/foo_spec.rb"], str(tmp_path))
        assert "rspec" in cmd
        assert "spec/models/foo_spec.rb" in cmd

    def test_kotlin(self, tmp_path):
        cmd = _build_test_command("kotlin", ["src/test/kotlin/FooTest.kt"], str(tmp_path))
        assert "gradlew" in cmd[0]

    def test_csharp(self, tmp_path):
        cmd = _build_test_command("csharp", ["src/FooTests.cs"], str(tmp_path))
        assert "dotnet" in cmd


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

    def test_empty_output(self):
        passed, failed, skipped = _parse_test_counts("", "")
        assert passed == 0
        assert failed == 0
        assert skipped == 0
