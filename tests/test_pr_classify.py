"""Tests for scripts/common/pr_classify.py — shared PR classification module.

Covers the public API that all three pipelines (MQ, PR-queue, PR-audit) rely on.
Exercises both BB-style and GH-style PR dicts to verify tracker-agnostic behavior.
"""
import pytest

from scripts.common.pr_classify import (
    SENSITIVE_PATHS,
    HIGH_BLAST_CATEGORIES,
    PR_TYPE_EMOJI,
    RISK_EMOJI,
    apply_blast_cap,
    build_category_breakdown,
    build_pr_metrics_table,
    build_work_type_distribution,
    classify_pr_category,
    classify_pr_flags,
    classify_pr_type,
    compute_complexity,
    compute_is_hotspot,
    compute_risk_score,
    enrich_pr_with_metrics,
    extract_issue_ref,
    is_test_file,
)


# ---------------------------------------------------------------------------
# Fixtures — canonical BB and GH PR dicts
# ---------------------------------------------------------------------------

def _bb_pr(**overrides):
    """Minimal Bitbucket-style PR dict."""
    pr = {
        "id": 42,
        "title": "feat: add payment gateway",
        "description": "Implements Stripe integration",
        "branch": "feature/payments",
        "source_branch": "feature/payments",
        "labels": [],
        "url": "https://bitbucket.org/acme/app/pull-requests/42",
        "blast_radius": "low",
    }
    pr.update(overrides)
    return pr


def _gh_pr(**overrides):
    """Minimal GitHub-style PR dict."""
    pr = {
        "number": 99,
        "title": "fix: null pointer in auth handler",
        "body": "Closes #55",
        "headRefName": "fix/auth-npe",
        "head_ref": "fix/auth-npe",
        "labels": [{"name": "bug"}],
        "url": "https://github.com/acme/app/pull/99",
        "blast_radius": "low",
    }
    pr.update(overrides)
    return pr


def _files(*specs):
    """Build file list from (path, additions, deletions) tuples."""
    return [{"path": p, "additions": a, "deletions": d} for p, a, d in specs]


# ---------------------------------------------------------------------------
# classify_pr_type — both schemas
# ---------------------------------------------------------------------------

class TestClassifyPrType:
    def test_security_keyword_wins(self):
        pr = _bb_pr(title="fix: patch CVE-2024-1234 vulnerability")
        assert classify_pr_type(pr) == "security"

    def test_label_bug(self):
        pr = _gh_pr(title="update login form", labels=[{"name": "bug"}])
        assert classify_pr_type(pr) == "bug"

    def test_conventional_commit(self):
        pr = _bb_pr(title="refactor(billing): extract payment module")
        assert classify_pr_type(pr) == "refactor"

    def test_title_keyword_feature(self):
        pr = _bb_pr(title="implement new dashboard widget")
        assert classify_pr_type(pr) == "feature"

    def test_branch_fallback_bug(self):
        pr = _gh_pr(title="some changes", labels=[], headRefName="bugfix/login-crash", head_ref="bugfix/login-crash")
        assert classify_pr_type(pr) == "bug"

    def test_bb_source_branch_used(self):
        pr = _bb_pr(title="some changes", branch="", source_branch="fix/payment-issue")
        assert classify_pr_type(pr) == "bug"

    def test_unknown_when_no_signal(self):
        pr = {"title": "hello world", "branch": "", "description": "", "labels": []}
        assert classify_pr_type(pr) == "unknown"

    def test_flags_test_pr(self):
        pr = _bb_pr(title="some code changes")
        flags = {"is_test_pr": True, "is_docs_pr": False, "is_wip_pr": False}
        assert classify_pr_type(pr, flags=flags) == "test"

    def test_flags_docs_pr(self):
        pr = _bb_pr(title="some code changes")
        flags = {"is_test_pr": False, "is_docs_pr": True, "is_wip_pr": False}
        assert classify_pr_type(pr, flags=flags) == "docs"


# ---------------------------------------------------------------------------
# classify_pr_category
# ---------------------------------------------------------------------------

class TestClassifyPrCategory:
    def test_file_path_confidence(self):
        pr = _bb_pr()
        files = _files(("src/auth/handler.go", 10, 5))
        cat, conf = classify_pr_category(pr, files=files)
        assert cat == "authn_authz"
        assert conf == "file_path"

    def test_label_confidence(self):
        pr = _gh_pr(labels=[{"name": "security"}])
        cat, conf = classify_pr_category(pr)
        assert cat == "security"
        assert conf == "label"

    def test_title_confidence(self):
        pr = _bb_pr(title="update database migration script")
        cat, conf = classify_pr_category(pr)
        assert cat == "data"
        assert conf == "title"

    def test_unknown_when_no_signal(self):
        pr = _bb_pr(title="misc changes", labels=[], description="")
        cat, conf = classify_pr_category(pr)
        assert cat == "unknown"
        assert conf == "unknown"

    def test_test_category_from_files(self):
        files = _files(("tests/test_auth.py", 50, 10), ("tests/test_billing.py", 30, 5))
        pr = _bb_pr()
        cat, conf = classify_pr_category(pr, files=files)
        assert cat == "test"
        assert conf == "file_path"


# ---------------------------------------------------------------------------
# classify_pr_flags
# ---------------------------------------------------------------------------

class TestClassifyPrFlags:
    def test_test_pr_from_files(self):
        files = _files(("tests/test_foo.py", 10, 0), ("tests/test_bar.py", 5, 0))
        flags = classify_pr_flags(_bb_pr(), files=files)
        assert flags["is_test_pr"] is True

    def test_docs_pr_from_files(self):
        files = _files(("docs/setup.md", 10, 0), ("README.md", 5, 0))
        flags = classify_pr_flags(_bb_pr(), files=files)
        assert flags["is_docs_pr"] is True

    def test_wip_title(self):
        flags = classify_pr_flags(_gh_pr(title="[WIP] new auth flow"))
        assert flags["is_wip_pr"] is True

    def test_branch_docs_prefix(self):
        flags = classify_pr_flags(_bb_pr(branch="docs/update-readme", source_branch="docs/update-readme"))
        assert flags["is_docs_pr"] is True

    def test_gh_headRefName_used(self):
        pr = _gh_pr(headRefName="test/e2e-suite", head_ref="test/e2e-suite")
        flags = classify_pr_flags(pr)
        assert flags["is_test_pr"] is True


# ---------------------------------------------------------------------------
# compute_risk_score
# ---------------------------------------------------------------------------

class TestComputeRiskScore:
    def test_small_pr_has_score(self):
        pr = _bb_pr(blast_radius="low", category="config")
        files = _files(("config/app.yaml", 5, 2))
        result = compute_risk_score(pr, files=files)
        assert result["risk_tier"] in ("low", "medium")
        assert result["risk_score"] > 0

    def test_high_risk_security_pr(self):
        pr = _bb_pr(title="fix: critical CVE vulnerability", blast_radius="high", category="security")
        files = _files(("src/auth/handler.go", 200, 50), ("src/crypto/keys.go", 100, 30))
        result = compute_risk_score(pr, files=files)
        assert result["risk_tier"] == "high"

    def test_no_files_neutral(self):
        pr = _bb_pr()
        result = compute_risk_score(pr)
        assert result["risk_tier"] in ("low", "medium")
        assert "risk_dimensions" in result

    def test_risk_dimensions_keys(self):
        pr = _gh_pr()
        result = compute_risk_score(pr, files=_files(("src/app.ts", 10, 5)))
        dims = result["risk_dimensions"]
        assert set(dims.keys()) == {"size", "file_count", "blast_radius", "sensitive_paths", "test_coverage", "historical"}


# ---------------------------------------------------------------------------
# apply_blast_cap
# ---------------------------------------------------------------------------

class TestApplyBlastCap:
    def test_test_pr_caps_to_low(self):
        assert apply_blast_cap("high", {"is_test_pr": True}) == "low"

    def test_docs_pr_caps_to_low(self):
        assert apply_blast_cap("medium", {"is_docs_pr": True}) == "low"

    def test_wip_caps_to_medium(self):
        assert apply_blast_cap("high", {"is_wip_pr": True}) == "medium"

    def test_no_cap_without_flags(self):
        assert apply_blast_cap("high", {"is_test_pr": False, "is_docs_pr": False, "is_wip_pr": False}) == "high"


# ---------------------------------------------------------------------------
# extract_issue_ref
# ---------------------------------------------------------------------------

class TestExtractIssueRef:
    def test_jira_key_from_title(self):
        pr = _bb_pr(title="PROJ-123 fix auth bug")
        ref = extract_issue_ref(pr)
        assert ref is not None
        assert ref["key"] == "PROJ-123"

    def test_gh_closes_from_body(self):
        pr = _gh_pr(title="fix auth", body="Closes #55")
        ref = extract_issue_ref(pr)
        assert ref is not None
        assert ref["key"] == "#55"

    def test_none_when_no_ref(self):
        pr = _bb_pr(title="misc changes", description="no issue")
        assert extract_issue_ref(pr) is None


# ---------------------------------------------------------------------------
# compute_complexity / compute_is_hotspot
# ---------------------------------------------------------------------------

class TestComputeComplexity:
    def test_low(self):
        assert compute_complexity(10, 2) == "low"

    def test_medium_boundary(self):
        assert compute_complexity(100, 6) == "medium"
        assert compute_complexity(50, 10) == "low"

    def test_high(self):
        assert compute_complexity(1000, 10) == "high"

    def test_zero(self):
        assert compute_complexity(0, 0) == "low"


class TestComputeIsHotspot:
    def test_hotspot_when_sensitive(self):
        assert compute_is_hotspot({"sensitive_paths": 3}) is True

    def test_not_hotspot_when_zero(self):
        assert compute_is_hotspot({"sensitive_paths": 0}) is False

    def test_not_hotspot_when_missing(self):
        assert compute_is_hotspot({}) is False


# ---------------------------------------------------------------------------
# enrich_pr_with_metrics — full enrichment
# ---------------------------------------------------------------------------

class TestEnrichPrWithMetrics:
    def test_bb_pr_with_files(self):
        pr = _bb_pr()
        files = _files(
            ("src/billing/charge.go", 100, 30),
            ("src/billing/refund.go", 50, 10),
            ("tests/test_charge.go", 20, 5),
        )
        enrich_pr_with_metrics(pr, files=files)
        assert pr["pr_type"] == "feature"
        assert pr["category"] in ("backend", "api", "test", "unknown")
        assert pr["blast_radius"] in ("low", "medium", "high")
        assert pr["risk_tier"] in ("low", "medium", "high")
        assert pr["total_loc"] == 215
        assert pr["file_count"] == 3
        assert pr["complexity"] in ("low", "medium", "high")
        assert isinstance(pr["is_hotspot"], bool)

    def test_gh_pr_without_files(self):
        pr = _gh_pr()
        enrich_pr_with_metrics(pr)
        assert pr["pr_type"] == "bug"
        assert pr["category"] is not None
        assert pr["risk_tier"] in ("low", "medium", "high")
        assert pr["total_loc"] == 0
        assert pr["file_count"] == 0
        assert pr["complexity"] == "low"

    def test_test_pr_category_override(self):
        pr = _bb_pr(title="[SDET] add auth tests")
        files = _files(("tests/test_auth.py", 100, 10), ("tests/test_billing.py", 50, 5))
        enrich_pr_with_metrics(pr, files=files)
        assert pr["is_test_pr"] is True
        assert pr["category"] == "test"
        assert pr["blast_radius"] == "low"

    def test_sensitive_path_hotspot(self):
        pr = _bb_pr(title="update auth handler")
        files = _files(("src/auth/handler.go", 50, 20))
        enrich_pr_with_metrics(pr, files=files)
        assert pr["is_hotspot"] is True

    def test_all_fields_present(self):
        pr = _bb_pr()
        enrich_pr_with_metrics(pr)
        required = [
            "pr_type", "category", "category_confidence", "blast_radius",
            "risk_score", "risk_tier", "risk_dimensions", "complexity",
            "is_hotspot", "total_loc", "file_count", "is_test_pr",
            "is_wip_pr", "is_docs_pr",
        ]
        for field in required:
            assert field in pr, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# Summary table builders
# ---------------------------------------------------------------------------

class TestBuildCategoryBreakdown:
    def test_empty_prs(self):
        assert build_category_breakdown([]) == []

    def test_all_unknown(self):
        prs = [{"category": "unknown", "pr_type": "unknown"}] * 5
        assert build_category_breakdown(prs) == []

    def test_basic_table(self):
        prs = [
            {"category": "api", "pr_type": "feature", "blast_radius": "low"},
            {"category": "api", "pr_type": "bug", "blast_radius": "low"},
            {"category": "security", "pr_type": "security", "risk_tier": "high"},
        ]
        lines = build_category_breakdown(prs)
        table_text = "\n".join(lines)
        assert "### Category Breakdown" in table_text
        assert "| api |" in table_text
        assert "| security |" in table_text
        assert "| Category | PRs | Bug PRs | High-Blast | Hotspot |" in table_text

    def test_hotspot_from_bug_count(self):
        prs = [{"category": "api", "pr_type": "bug"} for _ in range(4)]
        lines = build_category_breakdown(prs)
        table_text = "\n".join(lines)
        assert "🔥" in table_text

    def test_lanes_hotspot(self):
        prs = [{"category": "sre", "pr_type": "chore"}]
        lanes = [{"hotspots": ["sre"]}]
        lines = build_category_breakdown(prs, lanes=lanes)
        table_text = "\n".join(lines)
        assert "🔥 yes" in table_text


class TestBuildWorkTypeDistribution:
    def test_empty_prs(self):
        assert build_work_type_distribution([]) == []

    def test_all_unknown_returns_empty(self):
        prs = [{"pr_type": "unknown"}] * 5
        lines = build_work_type_distribution(prs)
        assert lines == []

    def test_basic_table(self):
        prs = [
            {"pr_type": "feature"},
            {"pr_type": "feature"},
            {"pr_type": "bug"},
        ]
        lines = build_work_type_distribution(prs)
        table_text = "\n".join(lines)
        assert "### Work Type Distribution" in table_text
        assert "feature" in table_text
        assert "bug" in table_text
        assert "66.7%" in table_text

    def test_type_order(self):
        prs = [{"pr_type": "docs"}, {"pr_type": "feature"}]
        lines = build_work_type_distribution(prs)
        feature_idx = next(i for i, l in enumerate(lines) if "feature" in l)
        docs_idx = next(i for i, l in enumerate(lines) if "docs" in l)
        assert feature_idx < docs_idx


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

class TestIsTestFile:
    def test_python_test(self):
        assert is_test_file("tests/test_auth.py") is True

    def test_go_test(self):
        assert is_test_file("pkg/handler_test.go") is True

    def test_js_test(self):
        assert is_test_file("src/app.test.ts") is True

    def test_regular_file(self):
        assert is_test_file("src/handler.go") is False


class TestSensitivePaths:
    def test_auth_path(self):
        assert SENSITIVE_PATHS.search("src/auth/handler.go") is not None

    def test_migration_path(self):
        assert SENSITIVE_PATHS.search("db/migrations/001.sql") is not None

    def test_safe_path(self):
        assert SENSITIVE_PATHS.search("src/utils/string.go") is None


# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_all_pr_types_have_emoji(self):
        for t in ("bug", "feature", "refactor", "chore", "security", "test", "docs", "unknown"):
            assert t in PR_TYPE_EMOJI

    def test_all_risk_tiers_have_emoji(self):
        for t in ("high", "medium", "low"):
            assert t in RISK_EMOJI

    def test_high_blast_categories(self):
        assert "security" in HIGH_BLAST_CATEGORIES
        assert "authn_authz" in HIGH_BLAST_CATEGORIES


# ---------------------------------------------------------------------------
# Category classification — test-file over-matching fix
# ---------------------------------------------------------------------------

class TestCategoryTestFileFiltering:
    """Verify that PRs with mixed source+test files aren't blindly classified as 'test'."""

    def test_mixed_files_classifies_from_source(self):
        files = _files(
            ("src/api/routes.ts", 50, 10),
            ("src/__tests__/routes.test.ts", 30, 5),
        )
        pr = _bb_pr(title="reclassify action-verb routes")
        cat, conf = classify_pr_category(pr, files=files)
        assert cat != "test", "Should not classify as test when source files present"
        assert conf == "file_path"

    def test_all_test_files_classifies_as_test(self):
        files = _files(
            ("tests/test_auth.py", 50, 10),
            ("tests/test_billing.py", 30, 5),
        )
        pr = _bb_pr()
        cat, conf = classify_pr_category(pr, files=files)
        assert cat == "test"

    def test_majority_test_files_classifies_as_test(self):
        files = _files(
            ("tests/test_a.py", 10, 0),
            ("tests/test_b.py", 10, 0),
            ("tests/test_c.py", 10, 0),
            ("tests/test_d.py", 10, 0),
            ("src/utils.py", 5, 0),
        )
        pr = _bb_pr()
        cat, conf = classify_pr_category(pr, files=files)
        assert cat == "test"

    def test_backend_files_with_test_files(self):
        files = _files(
            ("src/worker/handler.go", 80, 20),
            ("src/worker/handler_test.go", 40, 10),
        )
        pr = _bb_pr(title="rolling upgrade workers")
        cat, conf = classify_pr_category(pr, files=files)
        assert cat != "test"


# ---------------------------------------------------------------------------
# New bug keyword patterns
# ---------------------------------------------------------------------------

class TestNewBugKeywords:
    def test_flaky(self):
        assert classify_pr_type(_bb_pr(title="[Flaky Test] fix test input")) == "bug"

    def test_should_not(self):
        assert classify_pr_type(_bb_pr(title="TLS settings should not be displayed")) == "bug"

    def test_can_drop(self):
        assert classify_pr_type(_bb_pr(title="LogSearch grid can drop results")) == "bug"

    def test_silently_skips(self):
        assert classify_pr_type(_bb_pr(title="Rolling upgrade silently skips workers")) == "bug"

    def test_not_upgrading(self):
        assert classify_pr_type(_bb_pr(title="Worker not upgrading through outpost")) == "bug"

    def test_investigate(self):
        assert classify_pr_type(_bb_pr(title="Investigate Socket disconnect issue")) == "bug"

    def test_reclassify_is_refactor(self):
        assert classify_pr_type(_bb_pr(title="v2: reclassify action-verb routes")) == "refactor"

    def test_description_fallback_bug(self):
        pr = {"title": "misc changes", "description": "This fixes a regression in auth",
              "branch": "", "labels": []}
        assert classify_pr_type(pr) == "bug"

    def test_description_fallback_feature(self):
        pr = {"title": "misc changes", "description": "Implements new dashboard widget",
              "branch": "", "labels": []}
        assert classify_pr_type(pr) == "feature"


# ---------------------------------------------------------------------------
# New category title patterns
# ---------------------------------------------------------------------------

class TestNewCategoryTitlePatterns:
    def test_backend_worker(self):
        cat, conf = classify_pr_category(_bb_pr(title="Rolling upgrade silently skips workers"))
        assert cat == "backend"
        assert conf == "title"

    def test_backend_socket(self):
        cat, conf = classify_pr_category(_bb_pr(title="Investigate Socket disconnect issue"))
        assert cat == "backend"
        assert conf == "title"

    def test_api_routes(self):
        cat, conf = classify_pr_category(_bb_pr(title="reclassify action-verb routes"))
        assert cat == "api"
        assert conf == "title"

    def test_security_tls(self):
        cat, conf = classify_pr_category(_bb_pr(title="TLS settings should not be displayed"))
        assert cat == "security"
        assert conf == "title"

    def test_ui_grid(self):
        cat, conf = classify_pr_category(_bb_pr(title="LogSearch grid can drop results"))
        assert cat == "ui"
        assert conf == "title"

    def test_ui_font(self):
        cat, conf = classify_pr_category(_bb_pr(title="Change the font color in the tags"))
        assert cat == "ui"
        assert conf == "title"


# ---------------------------------------------------------------------------
# build_pr_metrics_table
# ---------------------------------------------------------------------------

class TestBuildPrMetricsTable:
    def test_empty(self):
        assert build_pr_metrics_table([]) == []

    def test_basic_table(self):
        prs = [
            {"pr_number": 123, "author": "alice", "category": "api",
             "pr_type": "bug", "blast_radius": "low", "risk_score": 25.0,
             "risk_tier": "medium", "total_loc": 150, "file_count": 5,
             "complexity": "medium", "is_hotspot": True,
             "url": "https://github.com/org/repo/pull/123",
             "title": "Fix payment validation",
             "linked_issue": {"key": "PROJ-42", "url": "https://jira.example.com/browse/PROJ-42"}},
            {"pr_number": 456, "author": "bob", "category": "ui",
             "pr_type": "feature", "blast_radius": "low", "risk_score": 10.0,
             "risk_tier": "low", "total_loc": 30, "file_count": 2,
             "complexity": "low", "is_hotspot": False,
             "url": "https://github.com/org/repo/pull/456",
             "title": "Add dashboard widget"},
        ]
        lines = build_pr_metrics_table(prs)
        text = "\n".join(lines)
        assert "### Per-PR Metrics" in text
        assert "[#123]" in text
        assert "[#456]" in text
        assert "🔥" in text
        assert "[PROJ-42]" in text
        assert "Fix payment" in text

    def test_sorted_by_risk_descending(self):
        prs = [
            {"pr_number": 1, "risk_score": 5.0, "risk_tier": "low",
             "author": "a", "category": "ui", "pr_type": "bug",
             "blast_radius": "low", "total_loc": 10, "file_count": 1,
             "complexity": "low", "is_hotspot": False},
            {"pr_number": 2, "risk_score": 50.0, "risk_tier": "high",
             "author": "b", "category": "api", "pr_type": "bug",
             "blast_radius": "high", "total_loc": 500, "file_count": 20,
             "complexity": "high", "is_hotspot": True},
        ]
        lines = build_pr_metrics_table(prs)
        text = "\n".join(lines)
        idx_pr2 = text.index("| 1 |")
        idx_pr1 = text.index("| 2 |")
        assert idx_pr2 < idx_pr1, "Higher risk PR should appear first (row 1)"

    def test_all_column_headers_present(self):
        prs = [{"pr_number": 1, "author": "a", "category": "api",
                "pr_type": "bug", "blast_radius": "low", "risk_score": 10.0,
                "risk_tier": "low", "total_loc": 10, "file_count": 1,
                "complexity": "low", "is_hotspot": False}]
        lines = build_pr_metrics_table(prs)
        header = lines[2]
        for col in ["PR", "Issue", "Author", "Title", "Cat", "Type", "Blast", "Risk", "LOC", "Files", "Cx"]:
            assert col in header, f"Missing column: {col}"
        assert "Hotspot" not in header, "Hotspot is shown as 🔥 prefix on Cat, not a separate column"

    def test_missing_fields_graceful(self):
        prs = [{"pr_number": 99}]
        lines = build_pr_metrics_table(prs)
        text = "\n".join(lines)
        assert "#99" in text


# ---------------------------------------------------------------------------
# HTML emoji annotation
# ---------------------------------------------------------------------------

class TestAnnotateEmoji:
    def test_wraps_known_emoji_in_span(self):
        from scripts.common.report_renderer import _annotate_emoji
        result = _annotate_emoji("🔴 high risk")
        assert '<span title="High">🔴</span>' in result

    def test_preserves_unknown_emoji(self):
        from scripts.common.report_renderer import _annotate_emoji
        result = _annotate_emoji("👍 approved")
        assert "👍" in result
        assert "<span" not in result

    def test_render_simple_html_includes_hover_labels(self):
        from scripts.common.report_renderer import render_simple_html
        html = render_simple_html("Test", "| Risk |\n|------|\n| 🔴 high |")
        assert 'title="High"' in html
        assert "🔴" in html
