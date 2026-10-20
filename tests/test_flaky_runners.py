"""Tests for scripts/flaky/detect_runner.py and scripts/flaky/identify.py."""

from __future__ import annotations

import json

import pytest

from scripts.flaky import detect_runner as dr
from scripts.flaky import identify as ident


def _write(p, text=""):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestDetect:
    @pytest.mark.parametrize("deps,expected", [
        ({"devDependencies": {"jest": "^29"}}, "jest"),
        ({"devDependencies": {"vitest": "^1"}}, "vitest"),
        ({"scripts": {"test": "mocha 'src/**/*.test.ts'"}}, "mocha"),
        ({"devDependencies": {"@playwright/test": "^1"}}, "playwright"),
    ])
    def test_node_frameworks(self, tmp_path, deps, expected):
        _write(tmp_path / "package.json", json.dumps(deps))
        _write(tmp_path / "src/a.test.ts")
        assert dr.detect_for_file(tmp_path, "src/a.test.ts")["runner"] == expected

    def test_playwright_vs_jest_prefers_path_hint(self, tmp_path):
        _write(tmp_path / "package.json", json.dumps({"devDependencies": {"jest": "1", "@playwright/test": "1"}}))
        assert dr.detect_for_file(tmp_path, "e2e/login.spec.ts")["runner"] == "playwright"
        assert dr.detect_for_file(tmp_path, "src/login.test.ts")["runner"] == "jest"

    @pytest.mark.parametrize("marker,expected", [
        ("pytest.ini", "pytest"), ("conftest.py", "pytest"), ("go.mod", "gotest"), ("pom.xml", "maven"),
        ("build.gradle.kts", "gradle"), ("Cargo.toml", "cargo"), ("app.csproj", "dotnet"),
    ])
    def test_manifest_markers(self, tmp_path, marker, expected):
        _write(tmp_path / marker, "[pytest]" if marker == "pytest.ini" else "")
        _write(tmp_path / "t/x")
        assert dr.detect_for_file(tmp_path, "t/x")["runner"] == expected

    def test_rspec_needs_spec_dir(self, tmp_path):
        _write(tmp_path / "Gemfile")
        assert dr.detect_for_file(tmp_path, "a.rb") is None
        (tmp_path / "spec").mkdir()
        assert dr.detect_for_file(tmp_path, "spec/a_spec.rb")["runner"] == "rspec"

    def test_monorepo_nearest_manifest_wins(self, tmp_path):
        _write(tmp_path / "package.json", json.dumps({"devDependencies": {"jest": "1"}}))
        _write(tmp_path / "services/api/go.mod")
        found = dr.detect_for_file(tmp_path, "services/api/handler_test.go")
        assert found["runner"] == "gotest" and found["root"].endswith("services/api")

    def test_unknown_returns_none(self, tmp_path):
        assert dr.detect_for_file(tmp_path, "whatever.txt") is None


class TestBuildCommands:
    def test_node_runners_isolate_by_name(self):
        assert dr.build_commands("jest", ".", "a.test.js", "adds item")["isolated"] == \
            ["npx", "--no-install", "jest", "a.test.js", "-t", "adds item"]
        assert dr.build_commands("mocha", ".", "a.test.js", "x")["isolated"][-2:] == ["--grep", "x"]
        assert dr.build_commands("playwright", ".", "a.spec.ts", "x")["isolated"][-2:] == ["-g", "x"]
        assert dr.build_commands("vitest", ".", "a.test.ts")["isolated"] is None

    def test_pytest_variants(self):
        assert dr.build_commands("pytest", ".", "t/test_a.py", "test_x")["isolated"][-1] == "t/test_a.py::test_x"
        assert dr.build_commands("pytest", ".", "t/test_a.py", "adds an item")["isolated"][-2:] == ["-k", "adds an item"]
        assert "no:cacheprovider" in dr.build_commands("pytest", ".", "t/test_a.py")["full"]

    def test_go_disables_cache_and_anchors_name(self):
        c = dr.build_commands("gotest", ".", "pkg/a_test.go", "TestX")
        assert c["full"] == ["go", "test", "-count=1", "./pkg"]
        assert c["isolated"] == ["go", "test", "-count=1", "-run", "^TestX$", "./pkg"]
        assert dr.build_commands("gotest", ".", "a_test.go", "TestX")["full"][-1] == "."

    def test_jvm_cargo_ruby_dotnet(self):
        assert dr.build_commands("maven", ".", "src/test/FooTest.java", "bar")["isolated"] == ["mvn", "-q", "-Dtest=FooTest#bar", "test"]
        assert dr.build_commands("gradle", ".", "FooTest.java", "bar")["isolated"][-1] == "FooTest.bar"
        assert dr.build_commands("cargo", ".", "tests/it.rs", "t1")["isolated"] == ["cargo", "test", "t1"]
        assert dr.build_commands("rspec", ".", "spec/a_spec.rb", "x")["isolated"][-2:] == ["-e", "x"]
        assert "FullyQualifiedName~Foo" in dr.build_commands("dotnet", ".", "a.cs", "Foo")["isolated"][-1]

    def test_test_name_is_one_argv_element_never_shell_text(self):
        evil = 'x"; rm -rf / #'
        iso = dr.build_commands("jest", ".", "a.test.js", evil)["isolated"]
        assert iso[-1] == evil and all(isinstance(a, str) for a in iso)

    def test_unsupported_runner_raises(self):
        with pytest.raises(ValueError, match="--cmd"):
            dr.build_commands("cobol", ".", "a")


class TestCli:
    def test_detect_runner_cli(self, tmp_path, capsys):
        _write(tmp_path / "go.mod")
        _write(tmp_path / "pkg/a_test.go")
        assert dr.main(["--repo", str(tmp_path), "--file", "pkg/a_test.go", "--name", "TestA"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["runner"] == "gotest" and out["isolated"][-1] == "./pkg"

    def test_undetected_exit_code(self, tmp_path, capsys):
        assert dr.main(["--repo", str(tmp_path), "--file", "x.txt"]) == 2
        assert json.loads(capsys.readouterr().out)["runner"] is None


class TestIdentify:
    def test_pytest_node_id(self):
        refs = ident.extract_test_refs("FAILED tests/test_cart.py::TestCart::test_limit - AssertionError")
        assert refs[0]["file"] == "tests/test_cart.py" and refs[0]["name"] == "TestCart::test_limit"

    def test_jest_fail_line_and_case(self):
        text = "FAIL src/cart.test.ts\n  ● Cart › rejects the 11th item\n"
        refs = ident.extract_test_refs(text)
        assert any(r["file"] == "src/cart.test.ts" for r in refs)
        assert any(r["name"] == "rejects the 11th item" and r["file"] == "src/cart.test.ts" for r in refs)

    def test_go_failure(self):
        refs = ident.extract_test_refs("--- FAIL: TestRetry (0.02s)\nFAIL\tgithub.com/acme/shop/pkg/retry\t0.5s")
        assert any(r["name"] == "TestRetry" for r in refs)

    def test_explicit_metadata_and_summary_form(self):
        refs = ident.extract_test_refs("[Flaky Test] file='src/core/queue.test.ts' failed 3 times")
        assert refs[0]["file"] == "src/core/queue.test.ts"

    def test_ignores_urls_and_non_test_files(self):
        refs = ident.extract_test_refs("see https://ci.example/job/1.test.js and path=src/main.py")
        assert refs == []

    def test_existing_files_ranked_first(self, tmp_path):
        _write(tmp_path / "real.test.js")
        refs = ident.extract_test_refs("FAIL ghost.test.js\nFAIL ghost.test.js\nFAIL real.test.js", tmp_path)
        assert refs[0]["file"] == "real.test.js" and refs[0]["exists"] is True
        assert refs[-1]["exists"] is False

    def test_empty(self):
        assert ident.extract_test_refs("") == []


class TestSafetyAndEdgeCases:
    @pytest.mark.parametrize("runner", ["jest", "vitest", "mocha", "playwright"])
    def test_test_names_are_escaped_for_the_runners_that_read_them_as_regexes(self, runner):
        iso = dr.build_commands(runner, ".", "a.test.js", "adds (item) [1] + more?")["isolated"]
        assert iso[-1] == r"adds \(item\) \[1\] \+ more\?"

    @pytest.mark.parametrize("kwargs", [{"test_file": "--config=evil.js"}, {"test_file": "a.js", "test_name": "--require=evil"}])
    def test_option_like_arguments_are_rejected(self, kwargs):
        with pytest.raises(ValueError, match="must not start with"):
            dr.build_commands("jest", ".", **{"test_file": "a.js", **kwargs})

    def test_cli_reports_option_injection_as_an_error(self, tmp_path, capsys):
        _write(tmp_path / "package.json", json.dumps({"devDependencies": {"jest": "1"}}))
        _write(tmp_path / "a.test.js")
        assert dr.main(["--repo", str(tmp_path), "--file", "a.test.js", "--name=--require=x"]) == 2
        assert "must not start with" in json.loads(capsys.readouterr().out)["error"]

    def test_sibling_directories_sharing_a_prefix_are_not_confused(self, tmp_path):
        _write(tmp_path / "go.mod")
        _write(tmp_path / "svc" / "a_test.go")
        _write(tmp_path / "svc-two" / "b_test.go")
        found = dr.detect_for_file(tmp_path, "svc-two/b_test.go")
        assert found["runner"] == "gotest"


class TestIdentifyRobustness:
    def test_hostile_input_finishes(self):
        import signal

        def boom(*_):
            raise TimeoutError

        old = signal.signal(signal.SIGALRM, boom)
        signal.alarm(10)
        try:
            for payload in ("a" * 1_000_000, "a/" * 200_000, "a.py::" * 100_000, "[" * 200_000, "x.test.js" * 50_000):
                ident.extract_test_refs(payload)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def test_files_starting_with_http_are_not_dropped(self):
        assert ident.extract_test_refs("FAILED http_client_test.py::test_x - boom")[0]["file"] == "http_client_test.py"
        assert ident.extract_test_refs("see https://ci.example/job/1/tests/a.test.js:3") == []

    def test_pytest_parameter_ids_with_spaces_are_kept(self):
        assert ident.extract_test_refs("FAILED t/test_a.py::test_x[a b-c] - boom")[0]["name"] == "test_x[a b-c]"

    def test_go_import_paths_are_not_reported_as_files(self):
        refs = ident.extract_test_refs("--- FAIL: TestRetry (0.02s)\nFAIL\tgithub.com/acme/shop/pkg/retry\t0.5s")
        assert [r["name"] for r in refs] == ["TestRetry"] and all("github.com" not in r["file"] for r in refs)

    def test_paths_outside_the_checkout_never_exist(self, tmp_path):
        outside = tmp_path.parent / "outside_test.py"
        outside.write_text("")
        refs = ident.extract_test_refs(f"FAILED ../{outside.name}::test_x\nFAILED /etc/passwd_test.py::test_y", tmp_path)
        assert all(r["exists"] is False for r in refs)
