"""Tests for scripts/mq/test_impact.py"""

import json
from pathlib import Path

import pytest

from scripts.mq._shared import is_test_file
from scripts.mq.test_impact import (
    _count_all_tests,
    _detect_language,
    _find_all_test_files,
    _map_to_test_files,
    _partition_shards,
    _should_skip,
)


class TestDetectLanguage:
    def test_python(self):
        assert _detect_language(["src/foo.py", "src/bar.py"]) == "python"

    def test_go(self):
        assert _detect_language(["pkg/foo.go", "cmd/main.go"]) == "go"

    def test_typescript(self):
        assert _detect_language(["src/app.ts", "src/util.tsx"]) == "typescript"

    def test_java(self):
        assert _detect_language(["src/main/java/Foo.java"]) == "java"

    def test_rust(self):
        assert _detect_language(["crates/core/src/lib.rs"]) == "rust"

    def test_kotlin(self):
        assert _detect_language(["src/main/kotlin/Foo.kt"]) == "kotlin"

    def test_ruby(self):
        assert _detect_language(["lib/foo.rb", "app/bar.rb"]) == "ruby"

    def test_csharp(self):
        assert _detect_language(["src/Foo.cs", "src/Bar.cs"]) == "csharp"

    def test_mixed_prefers_majority(self):
        files = ["a.py", "b.py", "c.py", "d.go"]
        assert _detect_language(files) == "python"

    def test_empty(self):
        assert _detect_language([]) == "python"


class TestIsTestFile:
    def test_python_test_prefix(self):
        assert is_test_file("test_foo.py")

    def test_python_test_in_tests_dir(self):
        assert is_test_file("tests/test_foo.py")

    def test_go_test(self):
        assert is_test_file("foo_test.go")

    def test_ts_test(self):
        assert is_test_file("foo.test.ts")

    def test_tsx_test(self):
        assert is_test_file("foo.test.tsx")

    def test_java_test(self):
        assert is_test_file("FooTest.java")

    def test_kotlin_test(self):
        assert is_test_file("FooTest.kt")

    def test_ruby_spec(self):
        assert is_test_file("foo_spec.rb")

    def test_ruby_test(self):
        assert is_test_file("foo_test.rb")

    def test_csharp_test(self):
        assert is_test_file("FooTests.cs")

    def test_spec_ts(self):
        assert is_test_file("foo.spec.ts")

    def test_spec_dir(self):
        assert is_test_file("spec/models/foo_spec.rb")

    def test_not_test(self):
        assert not is_test_file("foo.py")

    def test_not_test_go(self):
        assert not is_test_file("foo.go")


class TestMapToTestFiles:
    def test_python_mapping_no_repo(self):
        changed = ["charge.py"]
        found, unmapped = _map_to_test_files(changed, "python", None)
        assert len(found) > 0
        assert any("test_charge" in t for t in found)

    def test_python_mapping_with_repo(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_charge.py").write_text("# test")
        changed = ["charge.py"]
        found, unmapped = _map_to_test_files(changed, "python", str(tmp_path))
        assert "tests/test_charge.py" in found
        assert len(unmapped) == 0

    def test_unmapped_file(self, tmp_path):
        changed = ["src/billing/charge.py"]
        found, unmapped = _map_to_test_files(changed, "python", str(tmp_path))
        assert "src/billing/charge.py" in unmapped

    def test_test_file_passed_through(self):
        changed = ["test_foo.py"]
        found, unmapped = _map_to_test_files(changed, "python", None)
        assert "test_foo.py" in found

    def test_go_mapping(self):
        changed = ["pkg/billing/charge.go"]
        found, unmapped = _map_to_test_files(changed, "go", None)
        assert "pkg/billing/charge_test.go" in found

    def test_java_mapping(self):
        changed = ["src/main/java/com/Foo.java"]
        found, unmapped = _map_to_test_files(changed, "java", None)
        assert any("FooTest.java" in t for t in found)

    def test_kotlin_mapping(self):
        changed = ["src/main/kotlin/com/Foo.kt"]
        found, unmapped = _map_to_test_files(changed, "kotlin", None)
        assert any("FooTest.kt" in t for t in found)

    def test_ruby_mapping(self):
        changed = ["lib/billing/charge.rb"]
        found, unmapped = _map_to_test_files(changed, "ruby", None)
        assert any("charge_spec.rb" in t for t in found)

    def test_csharp_mapping(self):
        changed = ["src/Billing/Charge.cs"]
        found, unmapped = _map_to_test_files(changed, "csharp", None)
        assert any("ChargeTests.cs" in t for t in found)


class TestPartitionShards:
    def test_basic_partitioning(self):
        tests = ["test_a.py", "test_b.py", "test_c.py", "test_d.py"]
        shards = _partition_shards(tests, 2, None)
        assert len(shards) == 2
        total_tests = sum(s["test_count"] for s in shards)
        assert total_tests == 4

    def test_single_shard(self):
        tests = ["test_a.py", "test_b.py"]
        shards = _partition_shards(tests, 1, None)
        assert len(shards) == 1
        assert shards[0]["test_count"] == 2

    def test_more_shards_than_tests(self):
        tests = ["test_a.py"]
        shards = _partition_shards(tests, 5, None)
        assert len(shards) == 1

    def test_empty_tests(self):
        shards = _partition_shards([], 4, None)
        assert shards == []

    def test_with_timings(self):
        tests = ["fast.py", "slow.py"]
        timings = {"slow.py": 100.0, "fast.py": 5.0}
        shards = _partition_shards(tests, 2, timings)
        assert len(shards) == 2
        slow_shard = next(s for s in shards if "slow.py" in s["tests"])
        assert slow_shard["est_duration_s"] == 100.0

    def test_balanced_shards(self):
        tests = [f"test_{i}.py" for i in range(8)]
        timings = {f"test_{i}.py": float(i + 1) for i in range(8)}
        shards = _partition_shards(tests, 4, timings)
        durations = [s["est_duration_s"] for s in shards]
        assert max(durations) / min(durations) < 2.0

    def test_shard_ids_sequential(self):
        tests = [f"test_{i}.py" for i in range(6)]
        shards = _partition_shards(tests, 3, None)
        ids = [s["shard_id"] for s in shards]
        assert ids == [0, 1, 2]

    def test_zero_shards_clamped_to_one(self):
        tests = ["test_a.py"]
        shards = _partition_shards(tests, 0, None)
        assert len(shards) == 1


class TestShouldSkip:
    def test_node_modules(self):
        assert _should_skip(Path("node_modules/foo/test_bar.py"))

    def test_vendor(self):
        assert _should_skip(Path("vendor/github.com/pkg/foo_test.go"))

    def test_target(self):
        assert _should_skip(Path("target/debug/build/test.rs"))

    def test_git(self):
        assert _should_skip(Path(".git/hooks/pre-commit"))

    def test_pycache(self):
        assert _should_skip(Path("src/__pycache__/foo.cpython-311.pyc"))

    def test_normal_path(self):
        assert not _should_skip(Path("src/billing/charge.py"))

    def test_tests_dir(self):
        assert not _should_skip(Path("tests/test_charge.py"))


class TestFindAllTestFiles:
    def test_finds_test_files(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("")
        (tmp_path / "tests" / "test_b.py").write_text("")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("")
        result = _find_all_test_files(str(tmp_path))
        assert len(result) == 2
        assert "tests/test_a.py" in result

    def test_skips_node_modules(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("")
        (tmp_path / "node_modules" / "pkg" / "tests").mkdir(parents=True)
        (tmp_path / "node_modules" / "pkg" / "tests" / "test_x.py").write_text("")
        assert len(_find_all_test_files(str(tmp_path))) == 1

    def test_skips_vendor(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("")
        (tmp_path / "vendor" / "github.com").mkdir(parents=True)
        (tmp_path / "vendor" / "github.com" / "foo_test.go").write_text("")
        assert len(_find_all_test_files(str(tmp_path))) == 1

    def test_sorted_output(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_z.py").write_text("")
        (tmp_path / "tests" / "test_a.py").write_text("")
        result = _find_all_test_files(str(tmp_path))
        assert result == sorted(result)


class TestCountAllTests:
    def test_counts_test_files(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("")
        (tmp_path / "tests" / "test_b.py").write_text("")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("")
        assert _count_all_tests(str(tmp_path)) == 2

    def test_skips_node_modules(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("")
        (tmp_path / "node_modules" / "pkg" / "tests").mkdir(parents=True)
        (tmp_path / "node_modules" / "pkg" / "tests" / "test_x.py").write_text("")
        assert _count_all_tests(str(tmp_path)) == 1

    def test_empty_repo(self, tmp_path):
        assert _count_all_tests(str(tmp_path)) == 0

    def test_none_repo(self):
        assert _count_all_tests(None) == 0
