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
        assert any("billing" in lid for lid in lane_ids)
        assert any("auth" in lid for lid in lane_ids)

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
        assert any("default" in lid for lid in lane_ids)
        assert any("billing" in lid for lid in lane_ids)

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
        billing_lane = next(l for l in out["lanes"] if "billing" in l["lane_id"])
        ages = [p["age_hours"] for p in billing_lane["prs"]]
        assert ages == sorted(ages, reverse=True)

    def test_cross_branch_segregation(self, tmp_workspace):
        """PRs targeting different branches must land in different lanes."""
        ready_data = {
            "pr_count": 4,
            "prs": [
                {"pr_number": 1, "scope": "billing", "target_branch": "main", "age_hours": 5},
                {"pr_number": 2, "scope": "billing", "target_branch": "dev", "age_hours": 3},
                {"pr_number": 3, "scope": "auth", "target_branch": "main", "age_hours": 2},
                {"pr_number": 4, "scope": "billing", "target_branch": "dev", "age_hours": 1},
            ],
        }
        (tmp_workspace / "ready_prs.json").write_text(json.dumps(ready_data))

        runner = CliRunner()
        result = runner.invoke(main, [])
        assert result.exit_code == 0

        out = json.loads((tmp_workspace / "lane_groups.json").read_text())
        lane_ids = {l["lane_id"] for l in out["lanes"]}

        # Expect separate lanes for main and dev billing PRs
        assert "main/billing" in lane_ids, f"Expected 'main/billing' in {lane_ids}"
        assert "dev/billing" in lane_ids, f"Expected 'dev/billing' in {lane_ids}"
        assert "main/auth" in lane_ids, f"Expected 'main/auth' in {lane_ids}"

        # PR #1 and #3 (main) must not be in the same lane as PR #2 and #4 (dev)
        main_billing = next(l for l in out["lanes"] if l["lane_id"] == "main/billing")
        assert {p["pr_number"] for p in main_billing["prs"]} == {1}

        dev_billing = next(l for l in out["lanes"] if l["lane_id"] == "dev/billing")
        assert {p["pr_number"] for p in dev_billing["prs"]} == {2, 4}

    def test_multi_segment_target_branch(self, tmp_workspace):
        """Target branches with '/' in them (e.g. 'branches/AI-5005') form valid lane_ids.

        lane_id = '{target_branch}/{scope}' so 'branches/AI-5005/billing' is correct.
        rsplit('/', 1) must be used to extract the branch — split('/')[0] is wrong.
        """
        ready_data = {
            "pr_count": 2,
            "prs": [
                {"pr_number": 10, "scope": "billing", "target_branch": "branches/AI-5005", "age_hours": 5},
                {"pr_number": 11, "scope": "billing", "target_branch": "branches/AI-5005", "age_hours": 3},
            ],
        }
        (tmp_workspace / "ready_prs.json").write_text(json.dumps(ready_data))

        runner = CliRunner()
        result = runner.invoke(main, [])
        assert result.exit_code == 0

        out = json.loads((tmp_workspace / "lane_groups.json").read_text())
        lane_ids = [l["lane_id"] for l in out["lanes"]]
        assert "branches/AI-5005/billing" in lane_ids, (
            f"Expected 'branches/AI-5005/billing' in {lane_ids}"
        )

        lane = next(l for l in out["lanes"] if l["lane_id"] == "branches/AI-5005/billing")
        assert lane["pr_count"] == 2
        # rsplit("/", 1) must give the correct branch part
        for l in out["lanes"]:
            branch_part, scope_part = l["lane_id"].rsplit("/", 1)
            assert branch_part == "branches/AI-5005", f"Wrong branch part: {branch_part!r}"
            assert scope_part == "billing", f"Wrong scope part: {scope_part!r}"
