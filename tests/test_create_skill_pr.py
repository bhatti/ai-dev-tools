"""Tests for scripts/analyze/create_skill_pr.py"""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from scripts.analyze.create_skill_pr import (
    _write_empty_pr_json,
    _write_ygs_recommendations,
)


class TestWriteEmptyPrJson:
    def test_writes_empty_json(self, tmp_path):
        # artifacts module (get_issue_dir) uses workspace root directly — no issue_id subdir
        config = {"WORKSPACE_DIR": str(tmp_path)}
        _write_empty_pr_json(config)
        pr_json_path = tmp_path / "pr.json"
        assert pr_json_path.exists()
        pr_json = json.loads(pr_json_path.read_text())
        assert pr_json["url"] == ""
        assert pr_json["number"] == 0
        assert pr_json["branch"] == ""

    def test_overwrites_existing(self, tmp_path):
        (tmp_path / "pr.json").write_text('{"old": true}')
        config = {"WORKSPACE_DIR": str(tmp_path)}
        _write_empty_pr_json(config)
        pr_json = json.loads((tmp_path / "pr.json").read_text())
        assert "old" not in pr_json
        assert pr_json["url"] == ""


class TestWriteYgsRecommendations:
    def test_writes_recommendations(self, tmp_path):
        recs = [
            {"skill": "ygs-code-review", "recommendation": "Add security checklist"},
            {"skill": "ygs-implement", "recommendation": "Enforce test coverage"},
        ]
        _write_ygs_recommendations(tmp_path, recs)
        content = (tmp_path / "ygs_recommendations.md").read_text()
        assert "ygs-code-review" in content
        assert "Add security checklist" in content
        assert "ygs-implement" in content
        assert "Enforce test coverage" in content

    def test_empty_recommendations(self, tmp_path):
        _write_ygs_recommendations(tmp_path, [])
        content = (tmp_path / "ygs_recommendations.md").read_text()
        assert "YGS Skill Recommendations" in content


class TestMainNoImprovements:
    """Test main() early-exit paths."""

    @patch("scripts.analyze.create_skill_pr.load_config")
    @patch("scripts.analyze.create_skill_pr.get_workspace_dir")
    def test_no_improvements_file(self, mock_workspace, mock_config, tmp_path):
        mock_config.return_value = {"WORKSPACE_DIR": str(tmp_path)}
        mock_workspace.return_value = tmp_path
        reports = tmp_path / "reports"
        reports.mkdir()

        from scripts.analyze.create_skill_pr import main
        # main() should not raise when skill_improvements.json is missing
        main()

        # Should write empty pr.json
        assert (tmp_path / "pr.json").exists()
        pr_json = json.loads((tmp_path / "pr.json").read_text())
        assert pr_json["url"] == ""

    @patch("scripts.analyze.create_skill_pr.load_config")
    @patch("scripts.analyze.create_skill_pr.get_workspace_dir")
    def test_empty_changes(self, mock_workspace, mock_config, tmp_path):
        mock_config.return_value = {"WORKSPACE_DIR": str(tmp_path)}
        mock_workspace.return_value = tmp_path
        reports = tmp_path / "reports"
        reports.mkdir()

        # Write improvements with no actual changes
        improvements = {
            "repo_skill_changes": [],
            "new_docs": [],
            "ygs_recommendations": [
                {"skill": "ygs-test", "recommendation": "add more tests"},
            ],
        }
        (reports / "skill_improvements.json").write_text(json.dumps(improvements))

        from scripts.analyze.create_skill_pr import main
        main()

        # Should write empty pr.json and ygs_recommendations.md
        assert (tmp_path / "pr.json").exists()
        assert (reports / "ygs_recommendations.md").exists()
        pr_json = json.loads((tmp_path / "pr.json").read_text())
        assert pr_json["url"] == ""
