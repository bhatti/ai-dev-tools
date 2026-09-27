# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for group_by_scope hierarchical lane generation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_pr(pr_number: int, target_branch: str, blast_radius: str = "low",
             pr_type: str = "unknown", category: str = "unknown") -> dict:
    return {
        "pr_number": pr_number,
        "repo": "org/repo",
        "title": f"PR {pr_number}",
        "scope": "unknown",
        "blast_radius": blast_radius,
        "pr_type": pr_type,
        "category": category,
        "category_confidence": "unknown",
        "author": "dev",
        "age_hours": 1.0,
        "branch": f"feature/{pr_number}",
        "target_branch": target_branch,
        "ci_status": "success",
        "has_approval": True,
        "url": "",
        "labels": [],
    }


def _run_group(prs: list[dict], tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    (tmp_path / "ready_prs.json").write_text(
        json.dumps({"pr_count": len(prs), "repo": "org/repo", "target_branch_filter": "", "prs": prs})
    )
    from click.testing import CliRunner
    from scripts.mq.group_by_scope import main
    result = CliRunner().invoke(main, [])
    assert result.exit_code == 0, f"group_by_scope failed:\n{result.output}"
    return json.loads((tmp_path / "lane_groups.json").read_text())


class TestHierarchicalLanes:
    def test_canonical_branches_get_risk_tier_lanes(self, tmp_path, monkeypatch):
        prs = (
            [_make_pr(i, "main", "high") for i in range(1, 4)] +
            [_make_pr(i, "main", "medium") for i in range(4, 6)] +
            [_make_pr(i, "main", "low") for i in range(6, 8)]
        )
        data = _run_group(prs, tmp_path, monkeypatch)
        lane_ids = [l["lane_id"] for l in data["lanes"]]
        assert "main/high" in lane_ids
        assert "main/medium" in lane_ids
        assert "main/low" in lane_ids

    def test_feature_branch_prs_go_to_stacked(self, tmp_path, monkeypatch):
        prs = [_make_pr(1, "AI-2503-my-feature")]
        data = _run_group(prs, tmp_path, monkeypatch)
        lane_ids = [l["lane_id"] for l in data["lanes"]]
        assert any(lid.startswith("stacked/") for lid in lane_ids)
        assert "stacked/AI-2503-my-feature" in lane_ids

    def test_canonical_detection_by_count(self, tmp_path, monkeypatch):
        """Branch with >= CANONICAL_MIN_PRS PRs is treated as canonical even if not in default set."""
        monkeypatch.setenv("CANONICAL_MIN_PRS", "3")
        prs = [_make_pr(i, "release/v2.1", "low") for i in range(1, 5)]
        data = _run_group(prs, tmp_path, monkeypatch)
        lane_ids = [l["lane_id"] for l in data["lanes"]]
        assert any("release/v2.1" in lid and not lid.startswith("stacked/") for lid in lane_ids)

    def test_empty_risk_lanes_skipped(self, tmp_path, monkeypatch):
        prs = [_make_pr(1, "main", "high")]
        data = _run_group(prs, tmp_path, monkeypatch)
        lane_ids = [l["lane_id"] for l in data["lanes"]]
        assert "main/medium" not in lane_ids
        assert "main/low" not in lane_ids

    def test_canonical_env_override(self, tmp_path, monkeypatch):
        """CANONICAL_BRANCHES env var adds extra canonical branches."""
        monkeypatch.setenv("CANONICAL_BRANCHES", "custom-branch")
        prs = [_make_pr(1, "custom-branch", "low")]
        data = _run_group(prs, tmp_path, monkeypatch)
        lane_ids = [l["lane_id"] for l in data["lanes"]]
        assert "custom-branch/low" in lane_ids
        assert not any(lid.startswith("stacked/") for lid in lane_ids)

    def test_mixed_canonical_and_stacked(self, tmp_path, monkeypatch):
        prs = (
            [_make_pr(i, "dev", "low") for i in range(1, 4)] +
            [_make_pr(10, "feature/my-wip")]
        )
        data = _run_group(prs, tmp_path, monkeypatch)
        lane_ids = [l["lane_id"] for l in data["lanes"]]
        assert "dev/low" in lane_ids
        assert "stacked/feature/my-wip" in lane_ids

    def test_prs_in_lane_sorted_by_age_desc(self, tmp_path, monkeypatch):
        prs = [
            {**_make_pr(1, "main", "low"), "age_hours": 5.0},
            {**_make_pr(2, "main", "low"), "age_hours": 20.0},
            {**_make_pr(3, "main", "low"), "age_hours": 1.0},
        ]
        data = _run_group(prs, tmp_path, monkeypatch)
        low_lane = next(l for l in data["lanes"] if l["lane_id"] == "main/low")
        ages = [p["age_hours"] for p in low_lane["prs"]]
        assert ages == sorted(ages, reverse=True)

    def test_lane_has_category_counts(self, tmp_path, monkeypatch):
        """Each lane includes category_counts dict."""
        prs = [
            _make_pr(1, "main", "low", category="api"),
            _make_pr(2, "main", "low", category="api"),
            _make_pr(3, "main", "low", category="backend"),
        ]
        data = _run_group(prs, tmp_path, monkeypatch)
        lane = next(l for l in data["lanes"] if l["lane_id"] == "main/low")
        assert lane["category_counts"]["api"] == 2
        assert lane["category_counts"]["backend"] == 1

    def test_hotspot_detection(self, tmp_path, monkeypatch):
        """Lane with >=3 bug PRs in the same category flags it as a hotspot."""
        prs = [_make_pr(i, "main", "low", pr_type="bug", category="api") for i in range(1, 4)]
        data = _run_group(prs, tmp_path, monkeypatch)
        lane = next(l for l in data["lanes"] if l["lane_id"] == "main/low")
        assert "api" in lane["hotspots"]

    def test_no_hotspot_below_threshold(self, tmp_path, monkeypatch):
        """Fewer than 3 bug PRs in a category — no hotspot."""
        prs = [_make_pr(i, "main", "low", pr_type="bug", category="api") for i in range(1, 3)]
        data = _run_group(prs, tmp_path, monkeypatch)
        lane = next(l for l in data["lanes"] if l["lane_id"] == "main/low")
        assert lane["hotspots"] == []

    def test_total_prs_preserved(self, tmp_path, monkeypatch):
        prs = (
            [_make_pr(i, "main", "high") for i in range(1, 4)] +
            [_make_pr(i, "dev", "low") for i in range(4, 7)] +
            [_make_pr(10, "feature/wip")]
        )
        data = _run_group(prs, tmp_path, monkeypatch)
        assert data["total_prs"] == len(prs)
        lane_total = sum(l["pr_count"] for l in data["lanes"])
        assert lane_total == len(prs)
