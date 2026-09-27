"""Tests for scripts/mq/collect_ready.py"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from scripts.mq.collect_ready import _compute_age_hours, _classify_pr_type, _classify_pr_category, _normalize_pr, _enrich_prs_with_diffstat


class TestComputeAgeHours:
    def test_recent_pr(self):
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        age = _compute_age_hours(one_hour_ago)
        assert 0.9 < age < 1.2

    def test_old_pr(self):
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        age = _compute_age_hours(two_days_ago)
        assert 47 < age < 49

    def test_z_suffix(self):
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1))
        ts = one_hour_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
        age = _compute_age_hours(ts)
        assert 0.9 < age < 1.2

    def test_invalid_timestamp(self):
        assert _compute_age_hours("not-a-date") == 0.0

    def test_empty_string(self):
        assert _compute_age_hours("") == 0.0


class TestNormalizePr:
    """_normalize_pr handles differing field names between gather_gh and bb_helpers."""

    def test_github_pr(self):
        pr = {
            "number": 42,
            "title": "Fix billing bug",
            "author": "alice",
            "age_hours": 3.5,
            "headRefName": "fix/billing",
            "ci_status": "success",
            "has_approval": True,
            "url": "https://github.com/org/repo/pull/42",
            "labels": ["bugfix"],
        }
        result = _normalize_pr(pr, "org/repo")
        assert result["pr_number"] == 42
        assert result["author"] == "alice"
        assert result["age_hours"] == 3.5
        assert result["branch"] == "fix/billing"
        assert result["ci_status"] == "success"
        assert result["has_approval"] is True
        assert result["labels"] == ["bugfix"]

    def test_bitbucket_pr(self):
        pr = {
            "id": 99,
            "title": "BB feature",
            "author": "bob",
            "age_hours": 10.0,
            "branch": "feature/new",
        }
        result = _normalize_pr(pr, "ws/repo")
        assert result["pr_number"] == 99
        assert result["author"] == "bob"
        assert result["ci_status"] == "none"
        assert result["has_approval"] is False
        assert result["labels"] == []

    def test_dict_author_github_format(self):
        pr = {"number": 7, "author": {"login": "carol"}, "age_hours": 1.0}
        result = _normalize_pr(pr, "org/repo")
        assert result["author"] == "carol"

    def test_dict_author_bb_format(self):
        pr = {"id": 8, "author": {"display_name": "Dan Smith"}, "age_hours": 2.0}
        result = _normalize_pr(pr, "ws/repo")
        assert result["author"] == "Dan Smith"

    def test_approval_from_approval_count(self):
        pr = {"id": 5, "approval_count": 2, "age_hours": 1.0}
        result = _normalize_pr(pr, "ws/repo")
        assert result["has_approval"] is True

    def test_repo_fallback(self):
        pr = {"number": 1, "age_hours": 0.5}
        result = _normalize_pr(pr, "fallback/repo")
        assert result["repo"] == "fallback/repo"

    def test_pr_own_repo_takes_precedence(self):
        pr = {"number": 1, "repo": "own/repo", "age_hours": 0.5}
        result = _normalize_pr(pr, "fallback/repo")
        assert result["repo"] == "own/repo"

    def test_scope_always_unknown(self):
        """_enrich_prs_with_diffstat sets authoritative scope; collect_ready always emits 'unknown' initially."""
        pr = {"number": 3, "age_hours": 1.0}
        result = _normalize_pr(pr, "org/repo")
        assert result["scope"] == "unknown"

    def test_pr_type_field_present(self):
        pr = {"number": 5, "age_hours": 1.0, "title": "feat: add login", "labels": []}
        result = _normalize_pr(pr, "org/repo")
        assert "pr_type" in result
        assert result["pr_type"] == "feature"


class TestClassifyPrType:
    def test_bug_from_label_dict(self):
        pr = {"title": "some changes", "labels": [{"name": "bug"}]}
        assert _classify_pr_type(pr) == "bug"

    def test_bug_from_label_string(self):
        pr = {"title": "update something", "labels": ["hotfix"]}
        assert _classify_pr_type(pr) == "bug"

    def test_feature_from_label(self):
        pr = {"title": "some changes", "labels": [{"name": "feature"}]}
        assert _classify_pr_type(pr) == "feature"

    def test_feature_from_title(self):
        pr = {"title": "feat: add new dashboard", "labels": []}
        assert _classify_pr_type(pr) == "feature"

    def test_bug_from_title(self):
        pr = {"title": "fix: crash on empty input", "labels": []}
        assert _classify_pr_type(pr) == "bug"

    def test_unknown_when_no_signal(self):
        pr = {"title": "update readme", "labels": []}
        assert _classify_pr_type(pr) == "unknown"

    def test_label_takes_priority_over_title(self):
        pr = {"title": "feat: add billing fix", "labels": [{"name": "bug"}]}
        assert _classify_pr_type(pr) == "bug"


class TestClassifyPrCategory:
    """_classify_pr_category returns (category, confidence). File paths are authoritative."""

    def test_security_from_file_path(self):
        files = [{"path": "crypto/signing.py", "additions": 10, "deletions": 2}]
        cat, conf = _classify_pr_category({}, files)
        assert cat == "security"
        assert conf == "file_path"

    def test_authn_authz_from_auth_dir(self):
        """auth/ directory is authn_authz (more specific than generic security)."""
        files = [{"path": "auth/login.py", "additions": 5, "deletions": 0}]
        cat, conf = _classify_pr_category({}, files)
        assert cat == "authn_authz"
        assert conf == "file_path"

    def test_sre_from_terraform_path(self):
        files = [{"path": "terraform/modules/vpc.tf", "additions": 5, "deletions": 0}]
        cat, conf = _classify_pr_category({}, files)
        assert cat == "sre"
        assert conf == "file_path"

    def test_authn_authz_from_oauth_path(self):
        files = [{"path": "src/oauth/token_refresh.go", "additions": 20, "deletions": 5}]
        cat, conf = _classify_pr_category({}, files)
        assert cat == "authn_authz"
        assert conf == "file_path"

    def test_data_from_migration_path(self):
        files = [{"path": "migrations/0042_add_users.sql", "additions": 30, "deletions": 0}]
        cat, conf = _classify_pr_category({}, files)
        assert cat == "data"
        assert conf == "file_path"

    def test_api_from_label_when_no_files(self):
        pr = {"title": "update deps", "labels": [{"name": "api"}], "description": ""}
        cat, conf = _classify_pr_category(pr)
        assert cat == "api"
        assert conf == "label"

    def test_sre_from_title_when_no_files_no_labels(self):
        pr = {"title": "deploy new terraform module", "labels": [], "description": ""}
        cat, conf = _classify_pr_category(pr)
        assert cat == "sre"
        assert conf == "title"

    def test_unknown_when_no_signal(self):
        pr = {"title": "fix typo in readme", "labels": [], "description": ""}
        cat, conf = _classify_pr_category(pr)
        assert cat == "unknown"
        assert conf == "unknown"

    def test_file_path_overrides_label(self):
        """File path is authoritative even when label says something else."""
        pr = {"title": "some change", "labels": [{"name": "frontend"}], "description": ""}
        files = [{"path": "terraform/ecs.tf", "additions": 5, "deletions": 0}]
        cat, conf = _classify_pr_category(pr, files)
        assert cat == "sre"
        assert conf == "file_path"

    def test_category_present_in_normalize_pr(self):
        pr = {"number": 5, "age_hours": 1.0, "title": "deploy infra", "labels": []}
        result = _normalize_pr(pr, "org/repo")
        assert "category" in result
        assert "category_confidence" in result
        # title matches sre pattern
        assert result["category"] == "sre"
        assert result["category_confidence"] == "title"

    def test_enrich_sets_category_from_files(self):
        prs = [{
            "pr_number": 1, "blast_radius": "low", "scope": "unknown",
            "category": "unknown", "category_confidence": "unknown",
            "title": "some change", "labels": [], "description": "",
        }]
        files = [{"path": "terraform/main.tf", "additions": 10, "deletions": 0}]
        # Patch at source modules since _enrich_prs_with_diffstat imports lazily
        with patch("scripts.mq._shared.fetch_pr_files", return_value=files), \
             patch("scripts.mq.scope_router._compute_scope", return_value=("sre", "low", [], set())):
            _enrich_prs_with_diffstat(prs, {})
        assert prs[0]["category"] == "sre"
        assert prs[0]["category_confidence"] == "file_path"

    def test_enrich_keeps_defaults_on_empty_files(self):
        """Empty file list (no changed files) keeps existing label/title classification."""
        prs = [{
            "pr_number": 2, "blast_radius": "low", "scope": "unknown",
            "category": "api", "category_confidence": "label",
            "title": "", "labels": [], "description": "",
        }]
        with patch("scripts.mq._shared.fetch_pr_files", return_value=[]), \
             patch("scripts.mq.scope_router._compute_scope", return_value=("api", "low", [], set())):
            _enrich_prs_with_diffstat(prs, {})
        # Empty files list → _compute_scope not called → category unchanged
        assert prs[0]["category"] == "api"
        assert prs[0]["category_confidence"] == "label"

    def test_enrich_best_effort_on_error(self):
        """diffstat API failure leaves PR with its original classification — no exception raised."""
        prs = [{
            "pr_number": 3, "blast_radius": "low", "scope": "unknown",
            "category": "backend", "category_confidence": "title",
            "title": "", "labels": [], "description": "",
        }]
        with patch("scripts.mq._shared.fetch_pr_files", side_effect=RuntimeError("API timeout")):
            _enrich_prs_with_diffstat(prs, {})  # must not raise
        assert prs[0]["category"] == "backend"  # unchanged


class TestTargetBranchFilter:
    def test_target_branch_filter_in_ready_prs_json(self, tmp_path, monkeypatch):
        """collect_ready writes target_branch_filter to ready_prs.json."""
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        with patch("scripts.mq.collect_ready.fetch_open_prs", return_value=[]) as mock_fetch, \
             patch("scripts.mq.collect_ready._enrich_prs_with_diffstat"):
            from click.testing import CliRunner
            from scripts.mq.collect_ready import main
            result = CliRunner().invoke(main, ["--target-branch", "stage"])
            assert result.exit_code == 0, result.output
            data = json.loads((tmp_path / "ready_prs.json").read_text())
            assert data["target_branch_filter"] == "stage"
            # Verify target_branch was passed to fetch_open_prs
            call_kwargs = mock_fetch.call_args
            assert call_kwargs[1].get("target_branch") == "stage"

    def test_empty_target_branch_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        with patch("scripts.mq.collect_ready.fetch_open_prs", return_value=[]) as mock_fetch, \
             patch("scripts.mq.collect_ready._enrich_prs_with_diffstat"):
            from click.testing import CliRunner
            from scripts.mq.collect_ready import main
            result = CliRunner().invoke(main, [])
            assert result.exit_code == 0, result.output
            data = json.loads((tmp_path / "ready_prs.json").read_text())
            assert data["target_branch_filter"] == ""
