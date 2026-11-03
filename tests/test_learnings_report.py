"""Tests for scripts/analyze/learnings_report.py"""

from scripts.analyze.learnings_report import load_learnings, render_learnings_md

DATA = {
    "entries": [
        {"category": "security", "rule": "Check tenant on every query", "why": "cross-tenant leak", "look_for": "queries without tenant_id",
         "pr_count": 3, "prs": ["#1", "#2", "#3"], "reviewers": ["Alice"], "tribal": False},
        {"category": "Arch/Design", "rule": "Do not call the registry from handlers", "why": "caused a cycle in 2023", "look_for": "registry imports in handlers",
         "pr_count": 1, "prs": ["#9"], "reviewers": ["Alice"], "tribal": True},
    ],
    "reviewer_profiles": [{"reviewer": "Alice", "focus_areas": [{"category": "security", "count": 5}, {"category": "arch", "count": 2}],
                           "signature_concerns": ["Tenant isolation"], "quotes": [{"pr": "#1", "text": "who checks the tenant here?"}]}],
}


class TestRender:
    def test_groups_by_normalized_category_with_why_and_prs(self):
        md = render_learnings_md(DATA, requested=("Alice",), unmatched=[])
        assert "## Security" in md and "## Architecture" in md
        assert "Check tenant on every query" in md and "cross-tenant leak" in md and "#1, #2, #3" in md

    def test_tribal_section_and_reviewer_profile(self):
        md = render_learnings_md(DATA, requested=("Alice",), unmatched=[])
        assert "## Tribal knowledge" in md and "Do not call the registry" in md
        assert "## What Alice looks for" in md and "who checks the tenant here?" in md

    def test_unmatched_reviewers_warned(self):
        assert "Typo" in render_learnings_md({"entries": []}, requested=("Typo",), unmatched=["Typo"])

    def test_empty(self):
        assert "No learnings" in render_learnings_md({"entries": []}, requested=(), unmatched=[])


class TestLoad:
    def test_prefers_refined_and_tolerates_bad_json(self, tmp_path):
        (tmp_path / "review_learnings.json").write_text('{"entries": [{"rule": "raw"}]}')
        (tmp_path / "review_learnings_refined.json").write_text('{"entries": [{"rule": "refined"}]}')
        assert load_learnings(tmp_path)["entries"][0]["rule"] == "refined"
        (tmp_path / "review_learnings_refined.json").write_text("{bad")
        assert load_learnings(tmp_path)["entries"][0]["rule"] == "raw"
        (tmp_path / "review_learnings.json").unlink()
        assert load_learnings(tmp_path) == {"entries": [], "reviewer_profiles": []}
