"""Tests for scripts/analyze/create_skill_pr.py"""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from scripts.analyze.create_skill_pr import (
    _build_pr_body,
    _read_text_safe,
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


class TestBuildPrBody:
    """Tests for _build_pr_body — single function used by both GH and BB (DRY)."""

    def _changes(self):
        return [{"file_path": ".claude/skills/ygs-review.md", "description": "Add security section"}]

    def _docs(self):
        return [{"path": "docs/api-guide.md", "description": "API reference"}]

    def test_basic_body_contains_summary(self):
        body = _build_pr_body(self._changes(), self._docs())
        assert "## Summary" in body
        assert "Automated improvements" in body

    def test_audit_report_summary_extracted(self):
        audit = "# PR Audit Report\n\nThe team consistently skips security review.\n"
        body = _build_pr_body([], [], audit_report=audit)
        assert "The team consistently skips security review." in body

    def test_audit_report_in_details_block(self):
        audit = "## Findings\nSome findings here."
        body = _build_pr_body([], [], audit_report=audit)
        assert "<details>" in body
        assert "<summary>Full Audit Report</summary>" in body
        assert "Some findings here." in body
        assert "</details>" in body

    def test_skill_update_plan_section(self):
        plan = "## Priority 1\nUpdate ygs-review.md to add security checklist."
        body = _build_pr_body([], [], skill_update_plan=plan)
        assert "## Skill Update Plan" in body
        assert "ygs-review.md" in body

    def test_skill_update_plan_truncated(self):
        plan = "x" * 4000
        body = _build_pr_body([], [], skill_update_plan=plan)
        assert "truncated" in body

    def test_no_audit_report_no_details_block(self):
        body = _build_pr_body(self._changes(), self._docs())
        assert "<details>" not in body

    def test_no_plan_no_plan_section(self):
        body = _build_pr_body(self._changes(), self._docs())
        assert "## Skill Update Plan" not in body

    def test_skill_updates_listed(self):
        body = _build_pr_body(self._changes(), [])
        assert ".claude/skills/ygs-review.md" in body
        assert "Add security section" in body

    def test_new_docs_listed(self):
        body = _build_pr_body([], self._docs())
        assert "docs/api-guide.md" in body
        assert "API reference" in body

    def test_gh_and_bb_get_same_body(self):
        changes = self._changes()
        docs = self._docs()
        audit = "Full audit text."
        plan = "The plan."
        body1 = _build_pr_body(changes, docs, audit_report=audit, skill_update_plan=plan)
        body2 = _build_pr_body(changes, docs, audit_report=audit, skill_update_plan=plan)
        assert body1 == body2


class TestReadTextSafe:
    def test_returns_empty_for_missing(self, tmp_path):
        assert _read_text_safe(tmp_path / "nonexistent.md") == ""

    def test_returns_content_for_existing(self, tmp_path):
        p = tmp_path / "file.md"
        p.write_text("hello", encoding="utf-8")
        assert _read_text_safe(p) == "hello"
