# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for merge-queue scripts against real Bitbucket.

Requires env vars: BITBUCKET_WORKSPACE, BITBUCKET_REPO, BITBUCKET_USERNAME, BITBUCKET_TOKEN
All values come from environment — nothing is hardcoded.
Skip silently when creds absent.

Run:
    BITBUCKET_WORKSPACE=<ws> BITBUCKET_REPO=<repo> BITBUCKET_USERNAME=<user> \\
    BITBUCKET_TOKEN=<token> python3 -m pytest tests/test_mq_integration.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ── credential check ──────────────────────────────────────────────────────────

def _bb_config() -> dict:
    """Build a config from env vars. Returns empty dict if any cred is missing."""
    ws = os.environ.get("BITBUCKET_WORKSPACE", "")
    repo = os.environ.get("BITBUCKET_REPO", "")
    user = os.environ.get("BITBUCKET_USERNAME", "")
    token = os.environ.get("BITBUCKET_TOKEN", "")
    if not (ws and repo and user and token):
        return {}
    return {
        "BITBUCKET_WORKSPACE": ws,
        "BITBUCKET_REPO": repo,
        "BITBUCKET_USERNAME": user,
        "BITBUCKET_TOKEN": token,
        "DEFAULT_TRACKER": "bitbucket",
        "WORKSPACE_DIR": "/tmp/mq_integ_test",
        "STANDUP_TEAM_MEMBERS": "",
    }


def _require_bb_config() -> dict:
    cfg = _bb_config()
    if not cfg:
        pytest.skip("BB creds not set (BITBUCKET_WORKSPACE/REPO/USERNAME/TOKEN)")
    return cfg


# ── helpers ───────────────────────────────────────────────────────────────────

def _build_sys_path() -> None:
    """Ensure project root is in sys.path for imports."""
    root = Path(__file__).parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_build_sys_path()


# ── test 1: raw fetch has target_branch ───────────────────────────────────────

def test_bb_open_prs_have_target_branch() -> None:
    cfg = _require_bb_config()
    from scripts.mq._shared import fetch_open_prs

    prs = fetch_open_prs(cfg)
    if not prs:
        pytest.skip(f"No open PRs in {cfg['BITBUCKET_WORKSPACE']}/{cfg['BITBUCKET_REPO']}")

    missing = [pr for pr in prs if "target_branch" not in pr]
    assert not missing, (
        f"{len(missing)} PRs missing 'target_branch' field. First: {missing[0]}"
    )

    empty = [pr for pr in prs if not pr.get("target_branch")]
    print(f"\n[integ] fetched {len(prs)} PRs, {len(empty)} with empty target_branch")
    target_branches = sorted({pr["target_branch"] for pr in prs if pr.get("target_branch")})
    print(f"[integ] target branches seen: {target_branches}")
    for pr in prs[:5]:
        print(f"  PR #{pr.get('id')} '{pr.get('title','')[:40]}' → {pr.get('target_branch','?')}")


# ── test 2: normalize_pr preserves target_branch ──────────────────────────────

def test_bb_normalize_pr_target_branch() -> None:
    _require_bb_config()
    from scripts.mq.collect_ready import _normalize_pr

    raw_pr = {
        "id": 123,
        "title": "Test PR",
        "author": "alice",
        "created_on": "2025-01-01T00:00:00+00:00",
        "age_hours": 10.0,
        "reviewers": [],
        "url": "https://bitbucket.org/ws/repo/pull-requests/123",
        "target_branch": "dev",
        "branch": "feature/test",
    }
    normalized = _normalize_pr(raw_pr, "ws/repo")
    assert "target_branch" in normalized, f"target_branch missing from normalized: {normalized}"
    assert normalized["target_branch"] == "dev", (
        f"expected 'dev', got {normalized['target_branch']!r}"
    )

    # Also test that empty target_branch falls back gracefully
    raw_no_target = {**raw_pr, "target_branch": ""}
    normalized_no_target = _normalize_pr(raw_no_target, "ws/repo")
    assert "target_branch" in normalized_no_target


# ── test 3: collect_ready writes target_branch in ready_prs.json ─────────────

def test_bb_collect_ready_writes_target_branch() -> None:
    cfg = _require_bb_config()

    with tempfile.TemporaryDirectory() as tmpdir:
        env_patch = {
            "BITBUCKET_WORKSPACE": cfg["BITBUCKET_WORKSPACE"],
            "BITBUCKET_REPO": cfg["BITBUCKET_REPO"],
            "BITBUCKET_USERNAME": cfg["BITBUCKET_USERNAME"],
            "BITBUCKET_TOKEN": cfg["BITBUCKET_TOKEN"],
            "DEFAULT_TRACKER": "bitbucket",
            "WORKSPACE_DIR": tmpdir,
            "STANDUP_TEAM_MEMBERS": "",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            from click.testing import CliRunner
            from scripts.mq.collect_ready import main

            runner = CliRunner()
            result = runner.invoke(main, ["--label", ""])
            print(f"\n[integ] collect_ready output:\n{result.output}")
            if result.exit_code != 0:
                pytest.fail(f"collect_ready exited {result.exit_code}:\n{result.output}")

        ready_path = Path(tmpdir) / "ready_prs.json"
        assert ready_path.exists(), "ready_prs.json not written"
        data = json.loads(ready_path.read_text())
        prs = data.get("prs", [])
        if not prs:
            pytest.skip("No open PRs — cannot verify target_branch in output")

        missing = [pr for pr in prs if "target_branch" not in pr]
        assert not missing, f"{len(missing)} PRs missing target_branch in ready_prs.json"
        print(f"[integ] ready_prs.json has {len(prs)} PRs, all with target_branch ✓")


# ── test 4: group_by_scope segregates by target_branch ───────────────────────

def test_bb_group_by_scope_segregates_by_target_branch() -> None:
    cfg = _require_bb_config()

    with tempfile.TemporaryDirectory() as tmpdir:
        env_patch = {
            "BITBUCKET_WORKSPACE": cfg["BITBUCKET_WORKSPACE"],
            "BITBUCKET_REPO": cfg["BITBUCKET_REPO"],
            "BITBUCKET_USERNAME": cfg["BITBUCKET_USERNAME"],
            "BITBUCKET_TOKEN": cfg["BITBUCKET_TOKEN"],
            "DEFAULT_TRACKER": "bitbucket",
            "WORKSPACE_DIR": tmpdir,
            "STANDUP_TEAM_MEMBERS": "",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            from click.testing import CliRunner
            from scripts.mq.collect_ready import main as collect_main
            from scripts.mq.group_by_scope import main as group_main

            runner = CliRunner()

            # Step 1: collect
            result = runner.invoke(collect_main, ["--label", ""])
            if result.exit_code != 0:
                pytest.fail(f"collect_ready failed:\n{result.output}")

            ready_path = Path(tmpdir) / "ready_prs.json"
            data = json.loads(ready_path.read_text())
            prs = data.get("prs", [])
            if not prs:
                pytest.skip("No open PRs — cannot verify lane segregation")

            # Step 2: group
            result = runner.invoke(group_main, [])
            print(f"\n[integ] group_by_scope output:\n{result.output}")
            if result.exit_code != 0:
                pytest.fail(f"group_by_scope failed:\n{result.output}")

        lane_path = Path(tmpdir) / "lane_groups.json"
        assert lane_path.exists(), "lane_groups.json not written"
        lane_data = json.loads(lane_path.read_text())
        lanes = lane_data.get("lanes", [])
        assert lanes, "No lanes produced"

        # All lane_ids must be in format "{target_branch}/{scope}"
        for lane in lanes:
            lane_id = lane["lane_id"]
            assert "/" in lane_id, (
                f"lane_id {lane_id!r} missing target_branch prefix — expected format: '<branch>/<scope>'"
            )
            branch_part = lane_id.split("/")[0]
            assert branch_part, f"Empty branch part in lane_id {lane_id!r}"
            print(f"  lane {lane_id!r}: {lane['pr_count']} PRs")

        # If multiple target branches exist, verify no cross-branch mixing
        target_branches_in_lanes: dict[str, set[str]] = {}
        for lane in lanes:
            lane_id = lane["lane_id"]
            branch_prefix = lane_id.split("/")[0]
            for pr in lane.get("prs", []):
                tb = pr.get("target_branch", "") or "unknown"
                target_branches_in_lanes.setdefault(lane_id, set()).add(tb)

        for lane_id, branches in target_branches_in_lanes.items():
            branch_prefix = lane_id.split("/")[0]
            assert all(b == branch_prefix for b in branches), (
                f"Lane {lane_id!r} contains PRs targeting multiple branches: {branches}"
            )

        all_target_branches = sorted({l["lane_id"].split("/")[0] for l in lanes})
        print(f"\n[integ] {len(lanes)} lanes across target branches: {all_target_branches} ✓")
