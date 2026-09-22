"""Tests for scripts/mq/group_by_scope.py — integration test via CLI."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scripts.mq.group_by_scope import main


class TestGroupByScope:
    def test_groups_prs_by_scope(self, tmp_workspace):
        ready_data = {
            "pr_count": 4,
            "prs": [
                {"pr_number": 1, "scope": "billing", "age_hours": 10},
                {"pr_number": 2, "scope": "billing", "age_hours": 5},
                {"pr_number": 3, "scope": "auth", "age_hours": 8},
                {"pr_number": 4, "scope": "auth", "age_hours": 2},
            ],
        }
        (tmp_workspace / "ready_prs.json").write_text(json.dumps(ready_data))

        runner = CliRunner()
        result = runner.invoke(main, [])
        assert result.exit_code == 0

        out = json.loads((tmp_workspace / "lane_groups.json").read_text())
        assert out["lane_count"] == 2
        assert out["total_prs"] == 4
        lane_ids = {l["lane_id"] for l in out["lanes"]}
        assert "billing" in lane_ids
        assert "auth" in lane_ids

    def test_cross_scope_goes_to_default(self, tmp_workspace):
        ready_data = {
            "pr_count": 2,
            "prs": [
                {"pr_number": 1, "scope": "cross-scope", "age_hours": 10},
                {"pr_number": 2, "scope": "billing", "age_hours": 5},
            ],
        }
        (tmp_workspace / "ready_prs.json").write_text(json.dumps(ready_data))

        runner = CliRunner()
        result = runner.invoke(main, [])
        assert result.exit_code == 0

        out = json.loads((tmp_workspace / "lane_groups.json").read_text())
        lane_ids = {l["lane_id"] for l in out["lanes"]}
        assert "default" in lane_ids
        assert "billing" in lane_ids

    def test_missing_ready_prs_exits(self, tmp_workspace):
        runner = CliRunner()
        result = runner.invoke(main, [])
        assert result.exit_code != 0

    def test_sorted_by_age(self, tmp_workspace):
        ready_data = {
            "pr_count": 3,
            "prs": [
                {"pr_number": 1, "scope": "billing", "age_hours": 2},
                {"pr_number": 2, "scope": "billing", "age_hours": 10},
                {"pr_number": 3, "scope": "billing", "age_hours": 5},
            ],
        }
        (tmp_workspace / "ready_prs.json").write_text(json.dumps(ready_data))

        runner = CliRunner()
        result = runner.invoke(main, [])
        assert result.exit_code == 0

        out = json.loads((tmp_workspace / "lane_groups.json").read_text())
        billing_lane = next(l for l in out["lanes"] if l["lane_id"] == "billing")
        ages = [p["age_hours"] for p in billing_lane["prs"]]
        assert ages == sorted(ages, reverse=True)
