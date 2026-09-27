"""Cross-pipeline consistency tests for PR classification metrics.

Verifies that the same PR data produces identical classification results
regardless of which pipeline (MQ, PR queue, PR audit) processes it.
"""
from __future__ import annotations

import pytest

from scripts.common.pr_classify import (
    build_category_breakdown,
    build_work_type_distribution,
    classify_pr_category,
    classify_pr_flags,
    classify_pr_type,
    compute_complexity,
    compute_is_hotspot,
    compute_risk_score,
    enrich_pr_with_metrics,
    HIGH_BLAST_CATEGORIES,
    PR_TYPE_EMOJI,
    RISK_EMOJI,
)


# ---------------------------------------------------------------------------
# Canonical PR fixtures — one BB-style, one GH-style
# ---------------------------------------------------------------------------

def _bb_pr() -> dict:
    return {
        "id": "42",
        "title": "fix: auth token refresh crash",
        "description": "Fixes crash when token expires during request",
        "source_branch": "fix/auth-crash",
        "labels": [{"name": "bug"}, {"name": "security"}],
        "author": "alice",
    }


def _gh_pr() -> dict:
    return {
        "number": 99,
        "title": "fix: auth token refresh crash",
        "body": "Fixes crash when token expires during request",
        "headRefName": "fix/auth-crash",
        "labels": [{"name": "bug"}, {"name": "security"}],
        "author": "alice",
    }


_FILES = [
    {"path": "src/auth/token_refresh.go", "additions": 30, "deletions": 10},
    {"path": "src/auth/middleware.go", "additions": 15, "deletions": 5},
    {"path": "tests/auth/token_refresh_test.go", "additions": 40, "deletions": 0},
]


# ---------------------------------------------------------------------------
# Determinism: same input → same output
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_classify_pr_type_deterministic(self):
        results = {classify_pr_type(_bb_pr()) for _ in range(10)}
        assert len(results) == 1

    def test_classify_pr_category_deterministic(self):
        results = {classify_pr_category(_bb_pr(), _FILES)[0] for _ in range(10)}
        assert len(results) == 1

    def test_compute_risk_score_deterministic(self):
        results = {compute_risk_score(_bb_pr(), _FILES)["risk_score"] for _ in range(10)}
        assert len(results) == 1

    def test_enrich_deterministic(self):
        pr1, pr2 = _bb_pr(), _bb_pr()
        enrich_pr_with_metrics(pr1, files=_FILES)
        enrich_pr_with_metrics(pr2, files=_FILES)
        for key in ("pr_type", "category", "blast_radius", "risk_score", "risk_tier",
                     "complexity", "is_hotspot", "total_loc", "file_count"):
            assert pr1[key] == pr2[key], f"Mismatch on {key}: {pr1[key]} != {pr2[key]}"


# ---------------------------------------------------------------------------
# BB vs GH equivalence: same logical PR produces same classification
# ---------------------------------------------------------------------------

class TestTrackerEquivalence:
    def test_pr_type_same_for_bb_and_gh(self):
        assert classify_pr_type(_bb_pr()) == classify_pr_type(_gh_pr())

    def test_category_same_for_bb_and_gh(self):
        bb_cat, bb_conf = classify_pr_category(_bb_pr(), _FILES)
        gh_cat, gh_conf = classify_pr_category(_gh_pr(), _FILES)
        assert bb_cat == gh_cat
        assert bb_conf == gh_conf

    def test_risk_same_for_bb_and_gh(self):
        bb_risk = compute_risk_score(_bb_pr(), _FILES)
        gh_risk = compute_risk_score(_gh_pr(), _FILES)
        assert bb_risk["risk_score"] == gh_risk["risk_score"]
        assert bb_risk["risk_tier"] == gh_risk["risk_tier"]

    def test_flags_same_for_bb_and_gh(self):
        bb_flags = classify_pr_flags(_bb_pr(), _FILES)
        gh_flags = classify_pr_flags(_gh_pr(), _FILES)
        assert bb_flags == gh_flags

    def test_full_enrichment_same_for_bb_and_gh(self):
        bb, gh = _bb_pr(), _gh_pr()
        enrich_pr_with_metrics(bb, files=_FILES)
        enrich_pr_with_metrics(gh, files=_FILES)
        for key in ("pr_type", "category", "category_confidence", "blast_radius",
                     "risk_score", "risk_tier", "complexity", "is_hotspot",
                     "total_loc", "file_count"):
            assert bb[key] == gh[key], f"BB/GH mismatch on {key}: {bb[key]} != {gh[key]}"


# ---------------------------------------------------------------------------
# Taxonomy values are from canonical sets
# ---------------------------------------------------------------------------

_VALID_PR_TYPES = {"feature", "bug", "refactor", "chore", "docs", "test", "security", "unknown"}
_VALID_RISK_TIERS = {"low", "medium", "high"}
_VALID_BLAST_RADII = {"low", "medium", "high"}
_VALID_COMPLEXITIES = {"low", "medium", "high"}


class TestTaxonomyValues:
    @pytest.mark.parametrize("pr_factory", [_bb_pr, _gh_pr])
    def test_pr_type_in_canonical_set(self, pr_factory):
        assert classify_pr_type(pr_factory()) in _VALID_PR_TYPES

    @pytest.mark.parametrize("pr_factory", [_bb_pr, _gh_pr])
    def test_risk_tier_in_canonical_set(self, pr_factory):
        result = compute_risk_score(pr_factory(), _FILES)
        assert result["risk_tier"] in _VALID_RISK_TIERS

    @pytest.mark.parametrize("pr_factory", [_bb_pr, _gh_pr])
    def test_blast_radius_in_canonical_set(self, pr_factory):
        pr = pr_factory()
        enrich_pr_with_metrics(pr, files=_FILES)
        assert pr["blast_radius"] in _VALID_BLAST_RADII

    def test_complexity_boundaries(self):
        assert compute_complexity(100, 3) in _VALID_COMPLEXITIES
        assert compute_complexity(1000, 10) in _VALID_COMPLEXITIES
        assert compute_complexity(10000, 50) in _VALID_COMPLEXITIES

    def test_pr_type_emoji_covers_all_types(self):
        for t in _VALID_PR_TYPES:
            assert t in PR_TYPE_EMOJI, f"PR_TYPE_EMOJI missing key: {t}"

    def test_risk_emoji_covers_all_tiers(self):
        for t in _VALID_RISK_TIERS:
            assert t in RISK_EMOJI, f"RISK_EMOJI missing key: {t}"


# ---------------------------------------------------------------------------
# Summary tables — same output for same input regardless of pipeline
# ---------------------------------------------------------------------------

class TestSummaryTableConsistency:
    def _enriched_prs(self):
        prs = []
        for factory in (_bb_pr, _gh_pr):
            pr = factory()
            enrich_pr_with_metrics(pr, files=_FILES)
            prs.append(pr)
        return prs

    def test_category_breakdown_produces_markdown(self):
        lines = build_category_breakdown(self._enriched_prs())
        assert any("### Category Breakdown" in l for l in lines)
        assert any("|" in l for l in lines)

    def test_work_type_distribution_produces_markdown(self):
        lines = build_work_type_distribution(self._enriched_prs())
        assert any("### Work Type Distribution" in l for l in lines)
        assert any("|" in l for l in lines)

    def test_empty_prs_no_crash(self):
        assert build_category_breakdown([]) == []
        assert build_work_type_distribution([]) == []

    def test_unenriched_prs_no_crash(self):
        prs = [{"title": "misc", "author": "eve"}]
        lines = build_category_breakdown(prs)
        assert isinstance(lines, list)
        lines = build_work_type_distribution(prs)
        assert isinstance(lines, list)
