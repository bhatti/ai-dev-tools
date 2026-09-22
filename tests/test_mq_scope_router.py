"""Tests for scripts/mq/scope_router.py"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.mq.scope_router import (
    _compute_scope,
    _match_codeowner,
    _parse_codeowners,
    _top_level_module,
)


class TestTopLevelModule:
    def test_simple_path(self):
        assert _top_level_module("billing/charge.py") == "billing"

    def test_src_prefix(self):
        assert _top_level_module("src/billing/charge.py") == "src/billing"

    def test_lib_prefix(self):
        assert _top_level_module("lib/auth/login.py") == "lib/auth"

    def test_single_file(self):
        assert _top_level_module("README.md") == "README.md"

    def test_crates_prefix(self):
        assert _top_level_module("crates/core/src/lib.rs") == "crates/core"

    def test_apps_prefix(self):
        assert _top_level_module("apps/web/index.ts") == "apps/web"


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
