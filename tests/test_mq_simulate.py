"""Tests for scripts.mq.simulate — deployment pipeline simulation formulas."""
from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

import pytest

from scripts.mq.simulate import (
    MATURITY_DIMENSIONS,
    _MATURITY_MAX_SCORE,
    calamity_threshold,
    deployment_maturity_score,
    deployment_risk_summary,
    load_deployment_profile,
    merge_batch_success,
    release_train_success,
    rollback_feasibility,
)


class TestMergeBatchSuccess:
    def test_zero_defect_rate_returns_one(self):
        assert merge_batch_success(0.0, 100) == 1.0

    def test_full_defect_rate_returns_zero(self):
        assert merge_batch_success(1.0, 10) == 0.0

    def test_zero_batch_returns_one(self):
        assert merge_batch_success(0.05, 0) == 1.0

    def test_negative_batch_returns_one(self):
        assert merge_batch_success(0.05, -1) == 1.0

    def test_joe_reference_1_in_100_batch_25(self):
        result = merge_batch_success(0.01, 25)
        assert abs(result - 0.7778) < 0.01

    def test_joe_reference_1_in_40_batch_12(self):
        result = merge_batch_success(1 / 40, 12)
        expected = (1 - 1 / 40) ** 12
        assert abs(result - expected) < 0.0001

    def test_small_defect_large_batch(self):
        result = merge_batch_success(0.001, 500)
        assert 0.5 < result < 0.7


class TestReleaseTrainSuccess:
    def test_cd_equivalent_single_pr(self):
        assert release_train_success(0.02, 1) == merge_batch_success(0.02, 1)

    def test_weekly_train_50_prs_2pct_defect(self):
        result = release_train_success(0.02, 50)
        assert abs(result - (0.98 ** 50)) < 0.0001
        assert result < 0.4

    def test_daily_train_10_prs_1pct_defect(self):
        result = release_train_success(0.01, 10)
        assert abs(result - 0.9044) < 0.01


class TestRollbackFeasibility:
    def test_single_release_can_rollback(self):
        rb = rollback_feasibility(1)
        assert rb["can_rollback"] is True
        assert rb["strategy"] == "rollback"
        assert rb["mttr_multiplier"] == 1.0

    def test_two_releases_costly_rollback(self):
        rb = rollback_feasibility(2, avg_prs_per_release=15)
        assert rb["can_rollback"] is True
        assert rb["strategy"] == "rollback"
        assert rb["mttr_multiplier"] == 1.5
        assert rb["total_prs_at_risk"] == 30

    def test_three_releases_still_feasible(self):
        rb = rollback_feasibility(3)
        assert rb["can_rollback"] is True
        assert rb["mttr_multiplier"] == 1.5

    def test_four_releases_roll_forward_only(self):
        rb = rollback_feasibility(4)
        assert rb["can_rollback"] is False
        assert rb["strategy"] == "roll-forward"
        assert rb["mttr_multiplier"] == 2.5

    def test_many_stacked_releases(self):
        rb = rollback_feasibility(10, avg_prs_per_release=20)
        assert rb["can_rollback"] is False
        assert rb["total_prs_at_risk"] == 200
        assert "roll-forward" in rb["strategy"]


class TestDeploymentMaturityScore:
    def test_all_zeros_foundational(self):
        caps = {k: 0.0 for k in MATURITY_DIMENSIONS}
        result = deployment_maturity_score(caps)
        assert result["maturity_score"] == 0.0
        assert result["maturity_tier"] == "foundational"
        assert result["effective_risk_multiplier"] == 1.0

    def test_all_ones_advanced(self):
        caps = {k: 1.0 for k in MATURITY_DIMENSIONS}
        result = deployment_maturity_score(caps)
        assert result["maturity_score"] == _MATURITY_MAX_SCORE
        assert result["maturity_tier"] == "advanced"
        assert result["effective_risk_multiplier"] < 0.5

    def test_intermediate_tier(self):
        caps = {k: 0.5 for k in MATURITY_DIMENSIONS}
        result = deployment_maturity_score(caps)
        assert 4.0 <= result["maturity_score"] < 7.0
        assert result["maturity_tier"] == "intermediate"

    def test_missing_keys_default_zero(self):
        result = deployment_maturity_score({"automated_testing": 0.8})
        assert result["maturity_score"] == round(0.8 * 2.0, 1)
        assert result["maturity_tier"] == "foundational"

    def test_values_clamped_0_to_1(self):
        caps = {"automated_testing": 5.0, "canary_deployment": -1.0}
        result = deployment_maturity_score(caps)
        assert result["dimensions"]["automated_testing"]["value"] == 1.0
        assert result["dimensions"]["canary_deployment"]["value"] == 0.0

    def test_risk_multiplier_decreases_with_maturity(self):
        low = deployment_maturity_score({k: 0.1 for k in MATURITY_DIMENSIONS})
        high = deployment_maturity_score({k: 0.9 for k in MATURITY_DIMENSIONS})
        assert high["effective_risk_multiplier"] < low["effective_risk_multiplier"]

    def test_risk_multiplier_floor_at_03(self):
        caps = {k: 1.0 for k in MATURITY_DIMENSIONS}
        result = deployment_maturity_score(caps)
        assert result["effective_risk_multiplier"] >= 0.3


class TestCalamityThreshold:
    def test_low_defect_small_batch_green(self):
        result = calamity_threshold(0.001, 5)
        assert result["status"] == "green"
        assert result["current_success"] > 0.99

    def test_high_defect_large_batch_red(self):
        result = calamity_threshold(0.05, 50)
        assert result["status"] == "red"
        assert result["current_success"] < 0.70

    def test_max_safe_batch_formula(self):
        result = calamity_threshold(0.02, 10, target_success=0.70)
        expected_max = math.log(0.70) / math.log(0.98)
        assert abs(result["max_safe_batch"] - expected_max) < 0.2

    def test_zero_defect_infinite_headroom(self):
        result = calamity_threshold(0.0, 100)
        assert result["max_safe_batch"] is None
        assert result["headroom_pct"] == 100.0
        assert result["status"] == "green"

    def test_headroom_positive_when_below_max(self):
        result = calamity_threshold(0.02, 5)
        assert result["headroom_pct"] > 0

    def test_yellow_zone(self):
        dr = 0.02
        max_safe = math.log(0.70) / math.log(1 - dr)
        batch_just_below = max_safe * 0.6
        result = calamity_threshold(dr, batch_just_below)
        assert result["status"] in ("green", "yellow")


class TestDeploymentRiskSummary:
    def test_full_summary_structure(self):
        result = deployment_risk_summary(
            defect_rate=0.02,
            merge_batch_size=10,
            prs_per_release=50,
            maturity_capabilities={"automated_testing": 0.5},
            releases_stacked=3,
        )
        assert "merge_batch_success" in result
        assert "release_train_success" in result
        assert "adjusted_release_success" in result
        assert "rollback" in result
        assert "maturity" in result
        assert "calamity" in result
        assert "gauge" in result

    def test_adjusted_success_higher_than_raw_with_maturity(self):
        caps = {k: 0.8 for k in MATURITY_DIMENSIONS}
        result = deployment_risk_summary(
            defect_rate=0.02,
            merge_batch_size=10,
            prs_per_release=50,
            maturity_capabilities=caps,
            releases_stacked=1,
        )
        assert result["adjusted_release_success"] >= result["release_train_success"]

    def test_gauge_has_bar(self):
        result = deployment_risk_summary(
            defect_rate=0.01,
            merge_batch_size=5,
            prs_per_release=10,
            maturity_capabilities={},
            releases_stacked=1,
        )
        gauge = result["gauge"]
        assert "bar" in gauge
        assert "🔵" in gauge["bar"]


class TestLoadDeploymentProfile:
    def test_empty_returns_none(self):
        assert load_deployment_profile("") is None
        assert load_deployment_profile("  ") is None

    def test_preset_cd(self):
        p = load_deployment_profile("cd")
        assert p is not None
        assert p["prs_per_release"] == 1
        assert p["maturity"]["automated_testing"] == 1.0

    def test_preset_weekly_train_with_queue_size(self):
        p = load_deployment_profile("weekly-train", queue_size=200)
        assert p is not None
        assert p["prs_per_release"] == 200

    def test_preset_daily_train_with_queue_size(self):
        p = load_deployment_profile("daily-train", queue_size=100)
        assert p is not None
        assert p["prs_per_release"] == 20  # 100 / 5

    def test_preset_manual(self):
        p = load_deployment_profile("manual", queue_size=50)
        assert p is not None
        assert p["releases_stacked"] == 5
        assert p["maturity"]["canary_deployment"] == 0.0

    def test_preset_without_queue_size_defaults_to_1(self):
        p = load_deployment_profile("weekly-train", queue_size=0)
        assert p["prs_per_release"] == 1

    def test_unknown_name_no_file_returns_none(self):
        assert load_deployment_profile("nonexistent-preset") is None

    def test_json_file(self):
        profile_data = {
            "release_cadence": "custom",
            "prs_per_release": 30,
            "releases_stacked": 2,
            "maturity": {
                "automated_testing": 0.9,
                "canary_deployment": 0.5,
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(profile_data, f)
            f.flush()
            p = load_deployment_profile(f.name)
        assert p is not None
        assert p["prs_per_release"] == 30
        assert p["maturity"]["automated_testing"] == 0.9
        assert p["maturity"]["canary_deployment"] == 0.5
        assert p["maturity"]["blue_green"] == 0.0  # missing key defaults to 0

    def test_non_json_suffix_returns_none(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write('{"prs_per_release": 5}')
            f.flush()
            assert load_deployment_profile(f.name) is None

    def test_malformed_json_file_returns_none(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            f.flush()
            assert load_deployment_profile(f.name) is None

    def test_nonexistent_json_file_returns_none(self):
        assert load_deployment_profile("/tmp/does_not_exist_12345.json") is None

    def test_preset_mutation_isolation(self):
        p1 = load_deployment_profile("cd")
        p1["prs_per_release"] = 999
        p1["maturity"]["automated_testing"] = 0.0
        p2 = load_deployment_profile("cd")
        assert p2["prs_per_release"] == 1
        assert p2["maturity"]["automated_testing"] == 1.0
