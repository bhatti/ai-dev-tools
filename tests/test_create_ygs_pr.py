"""Unit tests for scripts.analyze.create_ygs_pr."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.analyze.create_ygs_pr import (
    _build_pr_body,
    _write_skipped_json,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path) -> dict:
    return {
        "WORKSPACE_DIR": str(tmp_path),
        "GH_TOKEN": "test-token",
        "GH_ORG": "myorg",
        "GH_REPO": "myrepo",
        "DEFAULT_TRACKER": "github",
    }


# ---------------------------------------------------------------------------
# _write_skipped_json
# ---------------------------------------------------------------------------

class TestWriteSkippedJson:
    def test_writes_skipped_status(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        _write_skipped_json(config, "insufficient evidence")
        # artifacts_write_json writes to {workspace}/{issue_id}/ygs_pr.json
        candidates = list(tmp_path.rglob("ygs_pr.json"))
        assert candidates, "ygs_pr.json not found"
        data = json.loads(candidates[0].read_text())
        assert data["status"] == "skipped"
        assert "insufficient evidence" in data["reason"]
        assert data["url"] == ""
        assert data["number"] == 0

    def test_does_not_write_pr_json(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        _write_skipped_json(config, "no data")
        # Must not write pr.json (that belongs to create_skill_pr)
        pr_files = list(tmp_path.rglob("pr.json"))
        assert not pr_files, "pr.json should not be written by create_ygs_pr"


# ---------------------------------------------------------------------------
# _build_pr_body
# ---------------------------------------------------------------------------

class TestBuildPrBody:
    def _make_qualified(self, count: int = 3) -> list[dict]:
        return [
            {
                "skill_name": f"ygs-skill-{i}",
                "section": f"Step {i}",
                "gap": f"Gap description {i}",
                "suggestion": f"Add check for pattern {i}",
                "pr_evidence_count": 4,
            }
            for i in range(count)
        ]

    def test_contains_source_repo(self, tmp_path: Path) -> None:
        body = _build_pr_body(self._make_qualified(), "my-source-repo", tmp_path, ["file1.md"])
        assert "my-source-repo" in body

    def test_lists_all_recommendations(self, tmp_path: Path) -> None:
        recs = self._make_qualified(3)
        body = _build_pr_body(recs, "repo", tmp_path, ["f.md"])
        for rec in recs:
            assert rec["skill_name"] in body

    def test_mentions_evidence_threshold(self, tmp_path: Path) -> None:
        body = _build_pr_body(self._make_qualified(), "repo", tmp_path, ["f.md"])
        assert "3+" in body or "3 PR" in body or "three" in body.lower() or "evidence" in body.lower()

    def test_empty_recs_produces_body(self, tmp_path: Path) -> None:
        body = _build_pr_body([], "repo", tmp_path, [])
        assert isinstance(body, str)
        assert len(body) > 0

    def test_contains_skill_name_in_table(self, tmp_path: Path) -> None:
        recs = [{"skill_name": "ygs-security-review", "gap": "SQL injection",
                 "suggestion": "add f-string check", "section": "Step 3", "pr_evidence_count": 5}]
        body = _build_pr_body(recs, "my-repo", tmp_path, ["ygs-security-review/SKILL.md"])
        assert "ygs-security-review" in body


# ---------------------------------------------------------------------------
# Evidence gate filter (inline logic test)
# ---------------------------------------------------------------------------

class TestEvidenceGate:
    """Test the pr_evidence_count >= 3 filter logic directly."""

    def test_filters_below_threshold(self) -> None:
        recs = [
            {"skill_name": "a", "pr_evidence_count": 2},
            {"skill_name": "b", "pr_evidence_count": 3},
            {"skill_name": "c", "pr_evidence_count": 5},
            {"skill_name": "d", "pr_evidence_count": 0},
        ]
        qualified = [r for r in recs if r.get("pr_evidence_count", 0) >= 3]
        assert len(qualified) == 2
        assert qualified[0]["skill_name"] == "b"
        assert qualified[1]["skill_name"] == "c"

    def test_missing_field_excluded(self) -> None:
        recs = [
            {"skill_name": "a"},  # no pr_evidence_count
            {"skill_name": "b", "pr_evidence_count": 3},
        ]
        qualified = [r for r in recs if r.get("pr_evidence_count", 0) >= 3]
        assert len(qualified) == 1
        assert qualified[0]["skill_name"] == "b"

    def test_all_below_threshold_empty_result(self) -> None:
        recs = [{"skill_name": "a", "pr_evidence_count": 1},
                {"skill_name": "b", "pr_evidence_count": 2}]
        qualified = [r for r in recs if r.get("pr_evidence_count", 0) >= 3]
        assert qualified == []

    def test_all_above_threshold(self) -> None:
        recs = [{"skill_name": str(i), "pr_evidence_count": 3 + i} for i in range(5)]
        qualified = [r for r in recs if r.get("pr_evidence_count", 0) >= 3]
        assert len(qualified) == 5


# ---------------------------------------------------------------------------
# main() — no improvements path
# ---------------------------------------------------------------------------

class TestMainNoImprovements:
    def test_no_skill_improvements_file(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        # No skill_improvements.json → should write skipped ygs_pr.json and exit 0
        with patch("scripts.analyze.create_ygs_pr.load_config", return_value=config):
            from scripts.analyze.create_ygs_pr import main
            main()  # should not raise
        candidates = list(tmp_path.rglob("ygs_pr.json"))
        assert candidates

    def test_empty_ygs_recommendations(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        (reports_dir / "skill_improvements.json").write_text(
            json.dumps({"repo_skill_changes": [], "ygs_recommendations": []}), encoding="utf-8"
        )
        with patch("scripts.analyze.create_ygs_pr.load_config", return_value=config):
            from scripts.analyze.create_ygs_pr import main
            main()
        candidates = list(tmp_path.rglob("ygs_pr.json"))
        assert candidates
        data = json.loads(candidates[0].read_text())
        assert data["status"] == "skipped"

    def test_all_recs_below_threshold(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        improvements = {
            "ygs_recommendations": [
                {"skill_name": "ygs-review-pr", "gap": "some gap",
                 "suggestion": "add check", "pr_evidence_count": 1},
                {"skill_name": "ygs-security-review", "gap": "another gap",
                 "suggestion": "add check", "pr_evidence_count": 2},
            ]
        }
        (reports_dir / "skill_improvements.json").write_text(
            json.dumps(improvements), encoding="utf-8"
        )
        with patch("scripts.analyze.create_ygs_pr.load_config", return_value=config):
            from scripts.analyze.create_ygs_pr import main
            main()
        candidates = list(tmp_path.rglob("ygs_pr.json"))
        assert candidates
        data = json.loads(candidates[0].read_text())
        assert data["status"] == "skipped"
        assert "insufficient evidence" in data["reason"].lower()
