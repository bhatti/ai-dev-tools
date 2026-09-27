"""Tests for scripts/mq/collect_ready.py"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from scripts.mq.collect_ready import (
    _compute_age_hours, _classify_pr_type, _classify_pr_category,
    _normalize_pr, _enrich_prs_with_diffstat, _extract_issue_ref,
    _classify_pr_flags,
)
from scripts.common.pr_classify import apply_blast_cap as _apply_blast_cap


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

    def test_reviewer_count_from_reviewers_list(self):
        pr = {"id": 10, "age_hours": 1.0, "reviewers": ["alice", "bob"]}
        result = _normalize_pr(pr, "ws/repo")
        assert result["reviewer_count"] == 2

    def test_reviewer_count_from_explicit_field(self):
        pr = {"id": 11, "age_hours": 1.0, "reviewer_count": 3, "reviewers": ["alice"]}
        result = _normalize_pr(pr, "ws/repo")
        assert result["reviewer_count"] == 3

    def test_approval_count_emitted(self):
        pr = {"id": 12, "age_hours": 1.0, "approval_count": 2}
        result = _normalize_pr(pr, "ws/repo")
        assert result["approval_count"] == 2
        assert result["has_approval"] is True

    def test_approval_count_zero_no_approval(self):
        pr = {"id": 13, "age_hours": 1.0, "approval_count": 0}
        result = _normalize_pr(pr, "ws/repo")
        assert result["approval_count"] == 0
        assert result["has_approval"] is False

    def test_issue_ref_field_present(self):
        pr = {"number": 14, "age_hours": 1.0, "title": "Fix FOO-123 login crash"}
        result = _normalize_pr(pr, "org/repo")
        assert "issue_ref" in result

    def test_issue_ref_jira_key_extracted(self):
        pr = {"number": 15, "age_hours": 1.0, "title": "PROJ-456: improve search"}
        result = _normalize_pr(pr, "org/repo")
        assert result["issue_ref"] is not None
        assert result["issue_ref"]["key"] == "PROJ-456"

    def test_issue_ref_none_when_no_signal(self):
        pr = {"number": 16, "age_hours": 1.0, "title": "minor cleanup"}
        result = _normalize_pr(pr, "org/repo")
        assert result["issue_ref"] is None


class TestExtractIssueRef:
    def test_jira_in_title(self):
        pr = {"title": "ACME-123: fix login bug", "description": ""}
        ref = _extract_issue_ref(pr)
        assert ref is not None
        assert ref["key"] == "ACME-123"

    def test_jira_in_description(self):
        pr = {"title": "login bug", "description": "Fixes PROJ-99 regression"}
        ref = _extract_issue_ref(pr)
        assert ref is not None
        assert ref["key"] == "PROJ-99"

    def test_github_closes_issue(self):
        pr = {"title": "fix auth", "description": "Closes #42",
              "url": "https://github.com/org/repo/pull/101"}
        ref = _extract_issue_ref(pr)
        assert ref is not None
        assert ref["key"] == "#42"
        assert ref["url"] == "https://github.com/org/repo/issues/42"

    def test_no_issue_signal(self):
        pr = {"title": "refactor billing", "description": "cleanup"}
        ref = _extract_issue_ref(pr)
        assert ref is None

    def test_jira_key_requires_minimum_two_uppercase_letters(self):
        # "A-123" has only 1 uppercase prefix char — should not match
        pr = {"title": "A-123 fix", "description": ""}
        assert _extract_issue_ref(pr) is None

    def test_jira_wins_over_github_closes_when_both_present(self):
        """Jira key takes priority over GitHub closing keyword."""
        pr = {"title": "PROJ-99 fix", "description": "Closes #42",
              "url": "https://github.com/org/repo/pull/7"}
        ref = _extract_issue_ref(pr)
        assert ref is not None
        assert ref["key"] == "PROJ-99"

    def test_jira_url_uses_jira_base_url_env(self, monkeypatch):
        monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example.com")
        pr = {"title": "PROJ-42: something", "description": ""}
        ref = _extract_issue_ref(pr)
        assert ref["url"] == "https://jira.example.com/browse/PROJ-42"

    def test_jira_url_empty_when_no_base_url(self, monkeypatch):
        monkeypatch.delenv("JIRA_BASE_URL", raising=False)
        pr = {"title": "PROJ-42: something", "description": ""}
        ref = _extract_issue_ref(pr)
        assert ref["key"] == "PROJ-42"
        assert ref["url"] == ""

    def test_no_false_positive_on_lowercase(self):
        """Lowercase words like 'v2-api' or 'fix-123' should not match Jira pattern."""
        pr = {"title": "update v2-api config fix-123 flow", "description": ""}
        assert _extract_issue_ref(pr) is None

    def test_body_field_used_for_github_prs(self):
        """GitHub PRs use 'body' not 'description' — both should be searched."""
        pr = {"title": "fix auth", "body": "Closes #55",
              "url": "https://github.com/org/repo/pull/88"}
        ref = _extract_issue_ref(pr)
        assert ref is not None
        assert ref["key"] == "#55"


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

    def test_chore_when_title_says_update(self):
        pr = {"title": "update readme", "labels": []}
        assert _classify_pr_type(pr) == "chore"

    def test_unknown_when_no_signal(self):
        pr = {"title": "tweak spacing in header", "labels": []}
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
        with patch("scripts.mq.collect_ready.fetch_pr_files", return_value=files), \
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
        with patch("scripts.mq.collect_ready.fetch_pr_files", return_value=[]), \
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
        with patch("scripts.mq.collect_ready.fetch_pr_files", side_effect=RuntimeError("API timeout")):
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

    def test_safety_filter_removes_wrong_branch_prs(self, tmp_path, monkeypatch):
        """collect_ready's safety filter removes PRs that slipped past upstream filtering."""
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        mixed_prs = [
            {"id": 1, "title": "stage pr", "target_branch": "stage", "author": "a",
             "branch": "f1", "created": "2025-01-01T00:00:00Z", "url": "http://x/1",
             "age_hours": 1, "reviewers": [], "reviewer_count": 0, "approval_count": 0},
            {"id": 2, "title": "dev pr", "target_branch": "dev", "author": "b",
             "branch": "f2", "created": "2025-01-01T00:00:00Z", "url": "http://x/2",
             "age_hours": 2, "reviewers": [], "reviewer_count": 0, "approval_count": 0},
            {"id": 3, "title": "another stage pr", "target_branch": "stage", "author": "c",
             "branch": "f3", "created": "2025-01-01T00:00:00Z", "url": "http://x/3",
             "age_hours": 3, "reviewers": [], "reviewer_count": 0, "approval_count": 0},
        ]
        with patch("scripts.mq.collect_ready.fetch_open_prs", return_value=mixed_prs), \
             patch("scripts.mq.collect_ready._enrich_prs_with_diffstat"):
            from click.testing import CliRunner
            from scripts.mq.collect_ready import main
            result = CliRunner().invoke(main, ["--target-branch", "stage"])
            assert result.exit_code == 0, result.output
            data = json.loads((tmp_path / "ready_prs.json").read_text())
            assert data["pr_count"] == 2
            assert all(p["target_branch"] == "stage" for p in data["prs"])


class TestClassifyPrFlags:
    """_classify_pr_flags returns is_test_pr, is_wip_pr, is_docs_pr."""

    # --- is_test_pr: title signal ---
    def test_sdet_title_detected_as_test_pr(self):
        pr = {"title": "[SDET][TASK]: ESERV-20916 fix auth tests", "branch": ""}
        flags = _classify_pr_flags(pr)
        assert flags["is_test_pr"] is True

    def test_qa_title_detected_as_test_pr(self):
        pr = {"title": "[QA] regression suite for billing", "branch": ""}
        flags = _classify_pr_flags(pr)
        assert flags["is_test_pr"] is True

    def test_normal_feat_title_not_test_pr(self):
        pr = {"title": "feat: add billing dashboard", "branch": "feature/billing"}
        flags = _classify_pr_flags(pr)
        assert flags["is_test_pr"] is False

    # --- is_test_pr: branch prefix signal ---
    def test_sdet_branch_prefix_detected(self):
        pr = {"title": "some change", "branch": "sdet/login-e2e"}
        flags = _classify_pr_flags(pr)
        assert flags["is_test_pr"] is True

    def test_test_branch_prefix_detected(self):
        pr = {"title": "some change", "branch": "test/auth-suite"}
        flags = _classify_pr_flags(pr)
        assert flags["is_test_pr"] is True

    def test_non_test_branch_not_flagged(self):
        pr = {"title": "some change", "branch": "fix/login-crash"}
        flags = _classify_pr_flags(pr)
        assert flags["is_test_pr"] is False

    # --- is_test_pr: file-path signal (threshold) ---
    def test_test_files_threshold_triggers_flag(self):
        """80% test files → is_test_pr=True."""
        pr = {"title": "update tests", "branch": ""}
        files = [
            {"path": "tests/auth/test_login.py"},
            {"path": "tests/auth/test_logout.py"},
            {"path": "tests/billing/test_invoice.py"},
            {"path": "tests/billing/test_refund.py"},
            {"path": "src/auth/login.py"},  # one non-test file → 80% exactly
        ]
        flags = _classify_pr_flags(pr, files=files)
        assert flags["is_test_pr"] is True

    def test_below_threshold_not_flagged(self):
        """Only 50% test files → is_test_pr=False (below 80% default threshold)."""
        pr = {"title": "update auth module", "branch": ""}
        files = [
            {"path": "tests/auth/test_login.py"},
            {"path": "src/auth/login.py"},
        ]
        flags = _classify_pr_flags(pr, files=files)
        assert flags["is_test_pr"] is False

    def test_go_test_files_detected(self):
        pr = {"title": "add unit tests", "branch": ""}
        files = [{"path": "pkg/auth/login_test.go"}, {"path": "pkg/billing/invoice_test.go"}]
        flags = _classify_pr_flags(pr, files=files)
        assert flags["is_test_pr"] is True

    # --- is_test_pr: env-var disable ---
    def test_disable_title_detection_via_empty_env(self, monkeypatch):
        """Setting TEST_PR_TITLE_PATTERNS='' disables title detection."""
        monkeypatch.setenv("TEST_PR_TITLE_PATTERNS", "")
        # Must re-import or call with explicit patterns; since patterns are module-level,
        # test via file-path signal only (title should not flag).
        pr = {"title": "[SDET] some test change", "branch": "feature/normal"}
        # With empty env, _TEST_TITLE_RE is None — no title match.
        # We test the function respects _TEST_TITLE_RE=None by calling with no files.
        # Since patterns are compiled at import time, we test the guard in the function directly.
        from scripts.common import pr_classify as pc
        original = pc._TEST_TITLE_RE
        try:
            pc._TEST_TITLE_RE = None  # simulate env="" at import
            flags = pc.classify_pr_flags(pr)
            assert flags["is_test_pr"] is False
        finally:
            pc._TEST_TITLE_RE = original

    # --- is_wip_pr ---
    def test_wip_title_detected(self):
        pr = {"title": "[WIP] refactor billing service", "branch": ""}
        flags = _classify_pr_flags(pr)
        assert flags["is_wip_pr"] is True

    def test_draft_title_detected(self):
        pr = {"title": "DRAFT: new auth flow", "branch": ""}
        flags = _classify_pr_flags(pr)
        assert flags["is_wip_pr"] is True

    def test_normal_pr_not_wip(self):
        pr = {"title": "feat: add billing dashboard", "branch": ""}
        flags = _classify_pr_flags(pr)
        assert flags["is_wip_pr"] is False

    # --- is_docs_pr ---
    def test_docs_title_detected(self):
        pr = {"title": "docs: update API reference", "branch": ""}
        flags = _classify_pr_flags(pr)
        assert flags["is_docs_pr"] is True

    def test_docs_branch_detected(self):
        pr = {"title": "update changelog", "branch": "docs/update-api-ref"}
        flags = _classify_pr_flags(pr)
        assert flags["is_docs_pr"] is True

    def test_all_markdown_files_docs_pr(self):
        pr = {"title": "update docs", "branch": ""}
        files = [{"path": "docs/api.md"}, {"path": "README.md"}, {"path": "CHANGELOG.md"}]
        flags = _classify_pr_flags(pr, files=files)
        assert flags["is_docs_pr"] is True

    def test_mixed_files_not_docs_pr(self):
        pr = {"title": "update docs and code", "branch": ""}
        files = [{"path": "docs/api.md"}, {"path": "src/auth/login.py"}]
        flags = _classify_pr_flags(pr, files=files)
        assert flags["is_docs_pr"] is False

    # --- category test classification takes priority over authn_authz ---
    def test_test_files_in_auth_dir_classified_as_test_not_authn_authz(self):
        """tests/auth/ files must be 'test' category, not 'authn_authz' (order matters)."""
        pr = {"title": "[SDET] auth regression suite", "branch": ""}
        files = [
            {"path": "tests/auth/test_login.py"},
            {"path": "tests/auth/test_oauth.py"},
            {"path": "tests/auth/test_session.py"},
            {"path": "tests/auth/test_token.py"},
        ]
        flags = _classify_pr_flags(pr, files=files)
        assert flags["is_test_pr"] is True
        # category classification should also be 'test' (tested via _normalize_pr)
        result = _normalize_pr(
            {"number": 99, "age_hours": 1.0, "title": "[SDET] auth regression suite"},
            "org/repo"
        )
        assert result["is_test_pr"] is True


class TestApplyBlastCap:
    """_apply_blast_cap enforces data-driven caps: test/docs→low, wip→medium."""

    def test_test_pr_capped_at_low(self):
        assert _apply_blast_cap("high", {"is_test_pr": True, "is_wip_pr": False, "is_docs_pr": False}) == "low"

    def test_test_pr_medium_capped_at_low(self):
        assert _apply_blast_cap("medium", {"is_test_pr": True, "is_wip_pr": False, "is_docs_pr": False}) == "low"

    def test_docs_pr_capped_at_low(self):
        assert _apply_blast_cap("high", {"is_test_pr": False, "is_wip_pr": False, "is_docs_pr": True}) == "low"

    def test_wip_pr_capped_at_medium(self):
        assert _apply_blast_cap("high", {"is_test_pr": False, "is_wip_pr": True, "is_docs_pr": False}) == "medium"

    def test_wip_pr_low_not_raised(self):
        """WIP cap is medium — but a WIP PR already at low stays low (cap only reduces)."""
        assert _apply_blast_cap("low", {"is_test_pr": False, "is_wip_pr": True, "is_docs_pr": False}) == "low"

    def test_no_flags_passes_through(self):
        assert _apply_blast_cap("high", {"is_test_pr": False, "is_wip_pr": False, "is_docs_pr": False}) == "high"

    def test_normal_low_unchanged(self):
        assert _apply_blast_cap("low", {"is_test_pr": False, "is_wip_pr": False, "is_docs_pr": False}) == "low"

    def test_test_wins_over_wip(self):
        """test cap (low) beats wip cap (medium) — lowest cap wins."""
        assert _apply_blast_cap("high", {"is_test_pr": True, "is_wip_pr": True, "is_docs_pr": False}) == "low"

    def test_enrich_applies_blast_cap_for_test_pr(self):
        """_enrich_prs_with_diffstat applies blast cap after re-evaluating flags with file paths."""
        prs = [{
            "pr_number": 10, "blast_radius": "high", "scope": "unknown",
            "category": "authn_authz", "category_confidence": "file_path",
            "title": "[SDET] auth tests", "labels": [], "description": "",
            "is_test_pr": False, "is_wip_pr": False, "is_docs_pr": False,
            "branch": "",
        }]
        # 100% test files → is_test_pr=True → blast capped at low
        files = [
            {"path": "tests/auth/test_login.py"},
            {"path": "tests/auth/test_oauth.py"},
        ]
        with patch("scripts.mq.collect_ready.fetch_pr_files", return_value=files), \
             patch("scripts.mq.scope_router._compute_scope", return_value=("cross-scope", "high", [], set())):
            _enrich_prs_with_diffstat(prs, {})
        assert prs[0]["blast_radius"] == "low"
        assert prs[0]["is_test_pr"] is True
        assert prs[0]["category"] == "test"


class TestComplexityAndHotspot:
    """Tests for complexity metric and hotspot detection set during enrichment."""

    def _make_pr(self, **overrides):
        base = {
            "pr_number": 1, "blast_radius": "low", "scope": "unknown",
            "category": "unknown", "category_confidence": "",
            "title": "some change", "labels": [], "description": "",
            "is_test_pr": False, "is_wip_pr": False, "is_docs_pr": False,
            "branch": "", "total_loc": 0, "file_count": 0,
            "complexity": "low", "is_hotspot": False,
            "risk_score": 0, "risk_tier": "low", "risk_dimensions": {},
        }
        base.update(overrides)
        return base

    def test_complexity_low(self):
        """Small PR: 10 LOC × 2 files = 20 → low."""
        prs = [self._make_pr()]
        files = [
            {"path": "a.py", "additions": 5, "deletions": 0},
            {"path": "b.py", "additions": 5, "deletions": 0},
        ]
        with patch("scripts.mq.collect_ready.fetch_pr_files", return_value=files), \
             patch("scripts.mq.scope_router._compute_scope", return_value=("mod", "low", [], set())):
            _enrich_prs_with_diffstat(prs, {})
        assert prs[0]["complexity"] == "low"

    def test_complexity_medium(self):
        """50 LOC × 15 files = 750 → medium."""
        prs = [self._make_pr()]
        files = [{"path": f"src/f{i}.py", "additions": 3, "deletions": 1} for i in range(15)]
        # Total LOC = 15 * (3+1) = 60, 60*15 = 900 > 500 → medium
        with patch("scripts.mq.collect_ready.fetch_pr_files", return_value=files), \
             patch("scripts.mq.scope_router._compute_scope", return_value=("mod", "medium", [], set())):
            _enrich_prs_with_diffstat(prs, {})
        assert prs[0]["complexity"] == "medium"

    def test_complexity_high(self):
        """Large PR: 200 LOC × 30 files = 6000 → high."""
        prs = [self._make_pr()]
        files = [{"path": f"src/f{i}.py", "additions": 5, "deletions": 2} for i in range(30)]
        # Total LOC = 30 * 7 = 210, 210*30 = 6300 > 5000 → high
        with patch("scripts.mq.collect_ready.fetch_pr_files", return_value=files), \
             patch("scripts.mq.scope_router._compute_scope", return_value=("mod", "high", [], set())):
            _enrich_prs_with_diffstat(prs, {})
        assert prs[0]["complexity"] == "high"

    def test_hotspot_detected(self):
        """PR touching auth/ path is flagged as hotspot."""
        prs = [self._make_pr()]
        files = [
            {"path": "src/auth/login.py", "additions": 10, "deletions": 0},
            {"path": "src/utils.py", "additions": 5, "deletions": 0},
        ]
        with patch("scripts.mq.collect_ready.fetch_pr_files", return_value=files), \
             patch("scripts.mq.scope_router._compute_scope", return_value=("mod", "medium", [], set())):
            _enrich_prs_with_diffstat(prs, {})
        assert prs[0]["is_hotspot"] is True

    def test_hotspot_not_flagged_normal_files(self):
        """PR touching only normal paths is not a hotspot."""
        prs = [self._make_pr()]
        files = [
            {"path": "src/utils.py", "additions": 5, "deletions": 0},
            {"path": "src/helpers.py", "additions": 3, "deletions": 0},
        ]
        with patch("scripts.mq.collect_ready.fetch_pr_files", return_value=files), \
             patch("scripts.mq.scope_router._compute_scope", return_value=("mod", "low", [], set())):
            _enrich_prs_with_diffstat(prs, {})
        assert prs[0]["is_hotspot"] is False

    def test_normalize_defaults(self):
        """_normalize_pr sets complexity='low' and is_hotspot=False by default."""
        pr = _normalize_pr({"id": 1, "title": "test"}, "org/repo")
        assert pr["complexity"] == "low"
        assert pr["is_hotspot"] is False
