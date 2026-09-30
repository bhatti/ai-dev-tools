"""Tests for scripts/mq/scope_router.py"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from scripts.mq._shared import top_level_module
from scripts.mq.scope_router import (
    _compute_scope,
    _match_codeowner,
    _parse_codeowners,
    main as _scope_router_main,
)


class TestTopLevelModule:
    def test_simple_path(self):
        assert top_level_module("billing/charge.py") == "billing"

    def test_src_prefix(self):
        assert top_level_module("src/billing/charge.py") == "src/billing"

    def test_lib_prefix(self):
        assert top_level_module("lib/auth/login.py") == "lib/auth"

    def test_single_file(self):
        assert top_level_module("README.md") == "README.md"

    def test_crates_prefix(self):
        assert top_level_module("crates/core/src/lib.rs") == "crates/core"

    def test_apps_prefix(self):
        assert top_level_module("apps/web/index.ts") == "apps/web"


class TestParseCodeowners:
    def test_no_repo_dir(self):
        assert _parse_codeowners(None) == {}

    def test_no_codeowners_file(self, tmp_path):
        assert _parse_codeowners(str(tmp_path)) == {}

    def test_standard_codeowners(self, tmp_path):
        co = tmp_path / "CODEOWNERS"
        co.write_text(
            "# comment\n"
            "billing/ @team-payments @lead-pay\n"
            "auth/ @team-security\n"
        )
        result = _parse_codeowners(str(tmp_path))
        assert "billing/" in result
        assert result["billing/"] == ["@team-payments", "@lead-pay"]
        assert result["auth/"] == ["@team-security"]

    def test_github_codeowners(self, tmp_path):
        gh = tmp_path / ".github"
        gh.mkdir()
        co = gh / "CODEOWNERS"
        co.write_text("src/ @team-core\n")
        result = _parse_codeowners(str(tmp_path))
        assert result["src/"] == ["@team-core"]


class TestMatchCodeowner:
    def test_simple_match(self):
        owners = _match_codeowner("billing/charge.py", {"billing/": ["@pay"]})
        assert owners == ["@pay"]

    def test_no_match(self):
        owners = _match_codeowner("frontend/app.ts", {"billing/": ["@pay"]})
        assert owners == []

    def test_last_match_wins(self):
        codeowners = {
            "src/": ["@general"],
            "src/billing/": ["@pay"],
        }
        owners = _match_codeowner("src/billing/charge.py", codeowners)
        assert owners == ["@pay"]

    def test_wildcard_match(self):
        owners = _match_codeowner("docs/guide.md", {"*.md": ["@docs"]})
        assert owners == ["@docs"]


class TestComputeScope:
    def _files(self, paths, additions=10, deletions=5):
        return [
            {"path": p, "additions": additions, "deletions": deletions}
            for p in paths
        ]

    def test_single_module_low_blast(self):
        files = self._files(["docs/guide.md", "docs/readme.md"], additions=3, deletions=2)
        scope, blast, sensitive, owners = _compute_scope(files, {})
        assert scope == "docs"
        assert blast == "low"
        assert sensitive == []

    def test_multi_module_medium_blast(self):
        files = self._files(["frontend/app.ts", "docs/readme.md"], additions=15, deletions=10)
        scope, blast, sensitive, owners = _compute_scope(files, {})
        assert scope == "cross-scope"
        assert blast == "medium"

    def test_high_blast_large_diff(self):
        files = self._files(["billing/charge.py"], additions=200, deletions=150)
        scope, blast, sensitive, owners = _compute_scope(files, {})
        assert blast == "high"

    def test_high_blast_many_modules(self):
        files = self._files([
            "billing/a.py", "auth/b.py", "frontend/c.py",
        ], additions=5, deletions=5)
        scope, blast, sensitive, owners = _compute_scope(files, {})
        assert blast == "high"

    def test_sensitive_paths_detected(self):
        files = self._files(["auth/tokens.py"])
        scope, blast, sensitive, owners = _compute_scope(files, {})
        assert len(sensitive) > 0
        assert "auth/tokens.py" in sensitive

    def test_codeowners_scope(self):
        files = self._files(["billing/charge.py", "billing/invoice.py"])
        codeowners = {"billing/": ["@team-payments"]}
        scope, blast, sensitive, owners = _compute_scope(files, codeowners)
        assert scope == "team-payments"
        assert "@team-payments" in owners

    def test_cross_scope_with_multiple_owners(self):
        files = self._files(["billing/a.py", "auth/b.py"])
        codeowners = {"billing/": ["@pay"], "auth/": ["@sec"]}
        scope, blast, sensitive, owners = _compute_scope(files, codeowners)
        assert scope == "cross-scope"
        assert "@pay" in owners
        assert "@sec" in owners


class TestScopeJsonOutput:
    """Verify scope.json written by main() contains all required fields."""

    _FILES = [
        {"path": "billing/charge.py", "additions": 30, "deletions": 10},
        {"path": "billing/invoice.py", "additions": 20, "deletions": 5},
    ]

    def _run(self, tmp_path, pr_meta=None):
        runner = CliRunner()
        with (
            patch("scripts.mq.scope_router.load_config", return_value={"WORKSPACE_DIR": str(tmp_path)}),
            patch("scripts.mq.scope_router.apply_repo_override"),
            patch("scripts.mq.scope_router.repo_slug", return_value="org/repo"),
            patch("scripts.mq.scope_router.fetch_pr_files", return_value=self._FILES),
            patch("scripts.analyze.pr_fetcher.fetch_single_pr", return_value=pr_meta or {}),
        ):
            result = runner.invoke(_scope_router_main, ["--pr-number", "42"])
        return result, json.loads((tmp_path / "scope.json").read_text())

    def test_separate_additions_deletions(self, tmp_path):
        _, data = self._run(tmp_path)
        assert data["additions"] == 50   # 30+20
        assert data["deletions"] == 15   # 10+5
        assert data["lines_changed"] == 65

    def test_categories_derived_from_paths(self, tmp_path):
        _, data = self._run(tmp_path)
        # billing/ paths → not empty; category derived from file paths
        assert isinstance(data["categories"], list)

    def test_pr_metadata_populated_when_available(self, tmp_path):
        meta = {"author": "alice", "created_at": "2026-09-01T00:00:00Z"}
        _, data = self._run(tmp_path, pr_meta=meta)
        assert data["author"] == "alice"
        assert data["created_at"] == "2026-09-01T00:00:00Z"

    def test_pr_metadata_empty_on_fetch_failure(self, tmp_path):
        runner = CliRunner()
        with (
            patch("scripts.mq.scope_router.load_config", return_value={"WORKSPACE_DIR": str(tmp_path)}),
            patch("scripts.mq.scope_router.apply_repo_override"),
            patch("scripts.mq.scope_router.repo_slug", return_value="org/repo"),
            patch("scripts.mq.scope_router.fetch_pr_files", return_value=self._FILES),
            patch("scripts.analyze.pr_fetcher.fetch_single_pr", side_effect=RuntimeError("api down")),
        ):
            runner.invoke(_scope_router_main, ["--pr-number", "42"])
        data = json.loads((tmp_path / "scope.json").read_text())
        assert data["author"] == ""
        assert data["created_at"] == ""
