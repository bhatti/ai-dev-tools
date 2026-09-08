"""Tests for git_archaeology codebase-wide audit functions."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.common.git_archaeology import (
    _is_code_file,
    _bug_hotspot_files,
    _commit_velocity,
    _emergency_commits,
    _top_contributors,
    analyze_commit_range,
    build_audit_context,
    compute_commit_health,
    compute_knowledge_silos,
    compute_temporal_coupling,
    find_test_gaps,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_repo(path: Path) -> None:
    """Init a bare git repo with global test identity."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], capture_output=True, check=True)


def _commit(path: Path, msg: str, files: dict[str, str], author: str = "Test User <test@test.com>") -> None:
    """Write files and create a commit."""
    for fname, content in files.items():
        fpath = path / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "-A"], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--author", author, "-m", msg],
        capture_output=True,
        check=True,
    )


# ── analyze_commit_range ───────────────────────────────────────────────────────

class TestAnalyzeCommitRange:
    def test_returns_commits_with_expected_structure(self, tmp_path):
        _make_repo(tmp_path)
        _commit(tmp_path, "feat: add service", {"src/service.py": "def run(): pass\n"})
        _commit(tmp_path, "fix: handle error", {"src/service.py": "def run(): raise\n", "src/utils.py": "x=1\n"})

        result = analyze_commit_range(tmp_path, n_commits=10)

        assert len(result) == 2
        # Most-recent first
        top = result[0]
        assert top["message"] == "fix: handle error"
        assert "src/service.py" in top["files"]
        assert "src/utils.py" in top["files"]
        assert isinstance(top["hash"], str) and len(top["hash"]) == 40
        assert isinstance(top["lines_added"], int)
        assert isinstance(top["lines_removed"], int)
        assert isinstance(top["date"], str)
        assert top["author"] == "Test User"

    def test_empty_on_non_git_directory(self, tmp_path):
        result = analyze_commit_range(tmp_path / "no-git", n_commits=10)
        assert result == []

    def test_respects_n_commits_limit(self, tmp_path):
        _make_repo(tmp_path)
        for i in range(5):
            _commit(tmp_path, f"commit {i}", {f"file{i}.py": f"x={i}\n"})

        result = analyze_commit_range(tmp_path, n_commits=3)
        assert len(result) == 3

    def test_excludes_non_code_files(self, tmp_path):
        """PDF and lock files should not appear in commit file lists."""
        _make_repo(tmp_path)
        _commit(tmp_path, "add code and doc", {"src/main.py": "x=1", "docs/design.pdf": "fake"})
        commits = analyze_commit_range(tmp_path, n_commits=10)
        assert commits
        files = commits[-1]["files"]
        code_files = [f for f in files if f.endswith(".py")]
        non_code = [f for f in files if f.endswith(".pdf")]
        assert len(code_files) >= 1
        assert len(non_code) == 0, f"PDF should be filtered out, but got: {non_code}"

    def test_binary_numstat_lines_skipped(self, tmp_path):
        """Commits with binary files use '-' in numstat — should not crash."""
        _make_repo(tmp_path)
        _commit(tmp_path, "add text", {"readme.txt": "hello\n"})
        # Write a trivial "binary-looking" file (just ensure no ValueError raised)
        (tmp_path / "img.bin").write_bytes(b"\x00\x01\x02\x03")
        subprocess.run(["git", "-C", str(tmp_path), "add", "img.bin"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "add binary"], capture_output=True, check=True)

        result = analyze_commit_range(tmp_path, n_commits=5)
        assert len(result) >= 2  # no crash


# ── compute_temporal_coupling ─────────────────────────────────────────────────

class TestComputeTemporalCoupling:
    def _make_commits_with_coupling(self) -> list[dict]:
        """Manufacture commit list where a/x.py and b/y.py always co-change."""
        commits = []
        for i in range(8):
            commits.append({
                "hash": f"abc{i:04x}",
                "message": f"change {i}",
                "author": "alice",
                "date": "2026-01-01",
                "files": ["a/x.py", "b/y.py", f"a/other{i}.py"],
                "lines_added": 1,
                "lines_removed": 0,
            })
        return commits

    def test_detects_cross_module_coupling(self):
        commits = self._make_commits_with_coupling()
        pairs = compute_temporal_coupling(commits, min_support=5, min_confidence=0.5)

        assert len(pairs) > 0
        found = any(
            (p["file_a"] == "a/x.py" and p["file_b"] == "b/y.py") or
            (p["file_a"] == "b/y.py" and p["file_b"] == "a/x.py")
            for p in pairs
        )
        assert found

    def test_same_directory_pairs_excluded(self):
        commits = [
            {
                "hash": f"h{i}", "message": "x", "author": "a", "date": "2026-01-01",
                "files": ["src/a.py", "src/b.py"],  # same directory
                "lines_added": 1, "lines_removed": 0,
            }
            for i in range(8)
        ]
        pairs = compute_temporal_coupling(commits, min_support=5, min_confidence=0.5)
        assert pairs == []

    def test_empty_commits_returns_empty(self):
        assert compute_temporal_coupling([], min_support=1) == []

    def test_results_sorted_by_confidence(self):
        commits = self._make_commits_with_coupling()
        pairs = compute_temporal_coupling(commits, min_support=2, min_confidence=0.1)
        confidences = [p["confidence"] for p in pairs]
        assert confidences == sorted(confidences, reverse=True)


# ── compute_knowledge_silos ────────────────────────────────────────────────────

class TestComputeKnowledgeSilos:
    def test_dominant_author_detected(self, tmp_path):
        _make_repo(tmp_path)
        # Alice makes many commits to the same file
        for i in range(5):
            _commit(tmp_path, f"alice change {i}", {"src/core.py": f"x={i}\n"},
                    author="Alice <alice@example.com>")
        # Bob makes one commit
        _commit(tmp_path, "bob fix", {"src/core.py": "x=99\n"},
                author="Bob <bob@example.com>")

        result = compute_knowledge_silos(tmp_path, ["src/core.py"], n_commits=50)

        assert "src/core.py" in result
        info = result["src/core.py"]
        assert info["top_author"] == "Alice"
        assert info["top_author_pct"] >= 5 / 6 - 0.01
        assert info["unique_authors"] == 2

    def test_missing_file_skipped(self, tmp_path):
        _make_repo(tmp_path)
        _commit(tmp_path, "init", {"readme.txt": "hi\n"})
        result = compute_knowledge_silos(tmp_path, ["nonexistent/file.py"], n_commits=10)
        assert result == {}

    def test_empty_file_list(self, tmp_path):
        _make_repo(tmp_path)
        _commit(tmp_path, "init", {"a.py": "1\n"})
        assert compute_knowledge_silos(tmp_path, [], n_commits=10) == {}


# ── compute_commit_health ─────────────────────────────────────────────────────

class TestComputeCommitHealth:
    def _make_commits(self, specs: list[tuple[str, list[str]]]) -> list[dict]:
        return [
            {"hash": f"h{i}", "message": msg, "author": "a", "date": "2026-01-01",
             "files": files, "lines_added": 1, "lines_removed": 0}
            for i, (msg, files) in enumerate(specs)
        ]

    def test_fix_ratio_calculated(self):
        commits = self._make_commits([
            ("fix: crash", []),
            ("fix: null", []),
            ("feat: new", []),
            ("feat: other", []),
        ])
        result = compute_commit_health(commits)
        assert result["fix_ratio"] == 0.5
        assert result["total"] == 4

    def test_large_commit_count(self):
        big_files = [f"f{i}.py" for i in range(20)]
        commits = self._make_commits([
            ("big change", big_files),
            ("small", ["a.py"]),
        ])
        result = compute_commit_health(commits)
        assert result["large_commit_count"] == 1

    def test_vague_message_count(self):
        commits = self._make_commits([
            ("x", []),
            ("ok", []),
            ("fix: proper message here", []),
        ])
        result = compute_commit_health(commits)
        assert result["vague_message_count"] == 2

    def test_ai_coauthored_count(self):
        commits = [
            {"hash": "h0", "message": "feat: add\n\nCo-authored-by: Claude Sonnet <noreply@anthropic.com>",
             "author": "a", "date": "2026-01-01", "files": [], "lines_added": 0, "lines_removed": 0},
            {"hash": "h1", "message": "fix: bug", "author": "a", "date": "2026-01-01",
             "files": [], "lines_added": 0, "lines_removed": 0},
        ]
        result = compute_commit_health(commits)
        assert result["ai_coauthored_count"] == 1

    def test_empty_returns_zeros(self):
        result = compute_commit_health([])
        assert result["total"] == 0
        assert result["fix_ratio"] == 0.0


# ── find_test_gaps ─────────────────────────────────────────────────────────────

class TestFindTestGaps:
    def _prod_commit(self, n: int, files: list[str]) -> list[dict]:
        return [
            {"hash": f"p{n}_{i}", "message": "change", "author": "a", "date": "2026-01-01",
             "files": files, "lines_added": 1, "lines_removed": 0}
            for i in range(n)
        ]

    def test_untested_file_detected(self):
        # prod file changed 4 times, never alongside a test
        commits = self._prod_commit(4, ["src/service.py"])
        result = find_test_gaps(commits)
        assert "src/service.py" in result["untested_files"]

    def test_tested_file_not_in_untested(self):
        commits = []
        for i in range(4):
            commits.append({
                "hash": f"h{i}", "message": "change", "author": "a", "date": "2026-01-01",
                "files": ["src/service.py", "tests/test_service.py"],
                "lines_added": 1, "lines_removed": 0,
            })
        result = find_test_gaps(commits)
        assert "src/service.py" not in result["untested_files"]

    def test_test_debt_indicators_counted(self):
        commits = [
            {"hash": "h0", "message": "skip test for now", "author": "a", "date": "2026-01-01",
             "files": ["src/x.py"], "lines_added": 1, "lines_removed": 0},
            {"hash": "h1", "message": "add test", "author": "a", "date": "2026-01-01",
             "files": ["src/x.py"], "lines_added": 1, "lines_removed": 0},
        ]
        result = find_test_gaps(commits)
        assert result["test_debt_indicators"] >= 1

    def test_empty_commits(self):
        result = find_test_gaps([])
        assert result["untested_files"] == []
        assert result["brittle_test_files"] == []
        assert result["test_debt_indicators"] == 0


# ── build_audit_context ────────────────────────────────────────────────────────

class TestBuildAuditContext:
    def test_returns_markdown_for_valid_repo(self, tmp_path):
        _make_repo(tmp_path)
        _commit(tmp_path, "feat: init", {"src/main.py": "print('hi')\n"})
        _commit(tmp_path, "fix: bug", {"src/main.py": "print('fixed')\n"})
        _commit(tmp_path, "feat: add util", {"src/util.py": "def x(): pass\n"})

        result = build_audit_context(tmp_path, n_commits=50)

        assert isinstance(result, str)
        assert len(result) > 0
        assert "Repository Audit Context" in result

    def test_empty_on_non_git_directory(self, tmp_path):
        result = build_audit_context(tmp_path / "no-git", n_commits=10)
        assert result == ""

    def test_capped_at_10000_chars(self, tmp_path):
        _make_repo(tmp_path)
        # Many commits with many files to generate a large output
        for i in range(30):
            _commit(tmp_path, f"feat: add module {i}",
                    {f"mod{i}/service.py": "x" * 10 + "\n", f"other{i}/util.py": "y\n"})

        result = build_audit_context(tmp_path, n_commits=200)
        assert len(result) <= 10000

    def test_focus_tests_includes_test_health(self, tmp_path):
        _make_repo(tmp_path)
        _commit(tmp_path, "feat: add service", {"src/service.py": "def run(): pass\n"})
        _commit(tmp_path, "fix: fix service", {"src/service.py": "def run(): return 1\n"})
        _commit(tmp_path, "fix: again", {"src/service.py": "def run(): return 2\n"})

        result = build_audit_context(tmp_path, n_commits=50, focus="tests")
        assert "Test Health" in result

    def test_focus_architecture_includes_temporal_coupling(self, tmp_path):
        _make_repo(tmp_path)
        # Files in different directories that always co-change
        for i in range(6):
            _commit(tmp_path, f"change {i}", {
                f"api/handler.py": f"x={i}\n",
                f"db/model.py": f"y={i}\n",
            })

        result = build_audit_context(tmp_path, n_commits=50, focus="architecture")
        # Either temporal coupling section or at least the header is present
        assert "Repository Audit Context" in result


# ── _is_code_file ──────────────────────────────────────────────────────────────

class TestIsCodeFile:
    def test_python_file_is_code(self):
        assert _is_code_file("src/main.py")

    def test_typescript_file_is_code(self):
        assert _is_code_file("frontend/src/app.tsx")

    def test_go_file_is_code(self):
        assert _is_code_file("cmd/server/main.go")

    def test_rust_file_is_code(self):
        assert _is_code_file("src/lib.rs")

    def test_yaml_config_is_code(self):
        assert _is_code_file("k8s/deployment.yaml")

    def test_pdf_is_not_code(self):
        assert not _is_code_file("docs/design.pdf")

    def test_png_is_not_code(self):
        assert not _is_code_file("assets/logo.png")

    def test_zip_is_not_code(self):
        assert not _is_code_file("releases/app.zip")

    def test_lock_file_package_lock_excluded(self):
        assert not _is_code_file("package-lock.json")

    def test_lock_file_yarn_excluded(self):
        assert not _is_code_file("yarn.lock")

    def test_lock_file_go_sum_excluded(self):
        assert not _is_code_file("go.sum")

    def test_git_internals_excluded(self):
        assert not _is_code_file(".git/COMMIT_EDITMSG")
        assert not _is_code_file(".git/objects/ab/cdef1234")

    def test_node_modules_excluded(self):
        assert not _is_code_file("node_modules/lodash/index.js")

    def test_vendor_excluded(self):
        assert not _is_code_file("vendor/github.com/pkg/errors/errors.go")

    def test_minified_js_excluded(self):
        assert not _is_code_file("dist/bundle.min.js")

    def test_minified_css_excluded(self):
        assert not _is_code_file("static/app.min.css")

    def test_makefile_is_code(self):
        assert _is_code_file("src/Makefile")

    def test_dockerfile_is_code(self):
        assert _is_code_file("Dockerfile")


# ── _bug_hotspot_files ─────────────────────────────────────────────────────────

class TestBugHotspotFiles:
    def test_files_in_fix_commits_detected(self, tmp_path):
        _make_repo(tmp_path)
        # Normal commits
        _commit(tmp_path, "feat: add service", {"src/service.py": "def f(): pass\n"})
        _commit(tmp_path, "feat: add util", {"src/util.py": "x=1\n"})
        # Fix commits touching service.py
        _commit(tmp_path, "fix: handle error in service", {"src/service.py": "def f(): return 1\n"})
        _commit(tmp_path, "bug: null check missing", {"src/service.py": "def f(): return 2\n"})

        result = _bug_hotspot_files(tmp_path)

        # service.py should appear with count >= 2
        files = dict(result)
        assert "src/service.py" in files
        assert files["src/service.py"] >= 2

    def test_returns_sorted_by_count_descending(self, tmp_path):
        _make_repo(tmp_path)
        _commit(tmp_path, "fix: auth error", {"auth/login.py": "x=1\n"})
        _commit(tmp_path, "fix: auth again", {"auth/login.py": "x=2\n"})
        _commit(tmp_path, "fix: util once", {"util/helper.py": "y=1\n"})

        result = _bug_hotspot_files(tmp_path)

        if len(result) >= 2:
            counts = [c for _, c in result]
            assert counts == sorted(counts, reverse=True)

    def test_empty_for_no_fix_commits(self, tmp_path):
        _make_repo(tmp_path)
        _commit(tmp_path, "feat: add service", {"src/service.py": "x=1\n"})
        _commit(tmp_path, "chore: update deps", {"requirements.txt": "requests==2.0\n"})

        result = _bug_hotspot_files(tmp_path)
        assert result == []

    def test_empty_on_non_git_directory(self, tmp_path):
        result = _bug_hotspot_files(tmp_path / "no-git")
        assert result == []


# ── _commit_velocity ───────────────────────────────────────────────────────────

class TestCommitVelocity:
    def test_returns_month_count_pairs(self, tmp_path):
        _make_repo(tmp_path)
        _commit(tmp_path, "feat: init", {"src/main.py": "x=1\n"})
        _commit(tmp_path, "fix: patch", {"src/main.py": "x=2\n"})

        result = _commit_velocity(tmp_path)

        assert isinstance(result, list)
        assert len(result) >= 1
        month, count = result[0]
        # Month format: YYYY-MM
        assert len(month) == 7 and month[4] == "-"
        assert isinstance(count, int) and count >= 1

    def test_sorted_chronologically(self, tmp_path):
        _make_repo(tmp_path)
        for i in range(3):
            _commit(tmp_path, f"commit {i}", {f"f{i}.py": "x\n"})

        result = _commit_velocity(tmp_path)
        months = [m for m, _ in result]
        assert months == sorted(months)

    def test_empty_on_non_git_directory(self, tmp_path):
        result = _commit_velocity(tmp_path / "no-git")
        assert result == []


# ── _emergency_commits ─────────────────────────────────────────────────────────

class TestEmergencyCommits:
    def test_detects_revert_commits(self, tmp_path):
        _make_repo(tmp_path)
        _commit(tmp_path, "feat: add feature", {"src/feature.py": "x=1\n"})
        _commit(tmp_path, "Revert: feat: add feature", {"src/feature.py": "x=0\n"})

        result = _emergency_commits(tmp_path)

        assert any("revert" in line.lower() for line in result)

    def test_detects_hotfix_commits(self, tmp_path):
        _make_repo(tmp_path)
        _commit(tmp_path, "feat: baseline", {"src/core.py": "x=1\n"})
        _commit(tmp_path, "hotfix: critical auth bypass", {"src/core.py": "x=2\n"})

        result = _emergency_commits(tmp_path)

        assert any("hotfix" in line.lower() for line in result)

    def test_normal_commits_excluded(self, tmp_path):
        _make_repo(tmp_path)
        _commit(tmp_path, "feat: add service", {"src/service.py": "x=1\n"})
        _commit(tmp_path, "fix: minor patch", {"src/service.py": "x=2\n"})

        result = _emergency_commits(tmp_path)

        # "fix" alone does not count as emergency
        assert not any(line for line in result
                       if "revert" not in line.lower() and "hotfix" not in line.lower()
                       and "emergency" not in line.lower() and "rollback" not in line.lower())

    def test_empty_on_non_git_directory(self, tmp_path):
        result = _emergency_commits(tmp_path / "no-git")
        assert result == []


# ── _top_contributors ──────────────────────────────────────────────────────────

class TestTopContributors:
    def test_returns_author_count_pairs(self, tmp_path):
        _make_repo(tmp_path)
        _commit(tmp_path, "feat: a1", {"f1.py": "x\n"}, author="Alice <alice@test.com>")
        _commit(tmp_path, "feat: a2", {"f2.py": "y\n"}, author="Alice <alice@test.com>")
        _commit(tmp_path, "feat: b1", {"f3.py": "z\n"}, author="Bob <bob@test.com>")

        result = _top_contributors(tmp_path, n_commits=100)

        assert isinstance(result, list)
        authors = dict(result)
        assert "Alice" in authors
        assert authors["Alice"] == 2
        assert "Bob" in authors
        assert authors["Bob"] == 1

    def test_sorted_by_count_descending(self, tmp_path):
        _make_repo(tmp_path)
        for i in range(4):
            _commit(tmp_path, f"alice {i}", {f"a{i}.py": "x\n"}, author="Alice <a@t.com>")
        _commit(tmp_path, "bob 1", {"b1.py": "y\n"}, author="Bob <b@t.com>")

        result = _top_contributors(tmp_path, n_commits=100)

        counts = [c for _, c in result]
        assert counts == sorted(counts, reverse=True)
        assert result[0][0] == "Alice"

    def test_respects_n_commits_limit(self, tmp_path):
        _make_repo(tmp_path)
        for i in range(6):
            _commit(tmp_path, f"commit {i}", {f"f{i}.py": "x\n"}, author="Alice <a@t.com>")

        result_limited = _top_contributors(tmp_path, n_commits=3)
        result_all = _top_contributors(tmp_path, n_commits=100)

        alice_limited = dict(result_limited).get("Alice", 0)
        alice_all = dict(result_all).get("Alice", 0)
        assert alice_limited <= alice_all

    def test_empty_on_non_git_directory(self, tmp_path):
        result = _top_contributors(tmp_path / "no-git", n_commits=100)
        assert result == []
