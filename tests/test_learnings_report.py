"""Tests for scripts/analyze/learnings_report.py"""

import json

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


class TestLoadSanitizesModelOutput:
    """review_learnings.json is written by an LLM: malformed shapes must be dropped or coerced, never crash later steps."""

    def _load(self, tmp_path, payload):
        (tmp_path / "review_learnings.json").write_text(json.dumps(payload))
        return load_learnings(tmp_path)

    def test_non_dict_entries_and_profiles_dropped(self, tmp_path):
        d = self._load(tmp_path, {"entries": ["a string", 3, None, {"rule": "r", "why": "w"}], "reviewer_profiles": ["x", {"reviewer": "A"}]})
        assert [e["rule"] for e in d["entries"]] == ["r"] and [p["reviewer"] for p in d["reviewer_profiles"]] == ["A"]

    def test_field_types_coerced(self, tmp_path):
        d = self._load(tmp_path, {"entries": [{"rule": "r", "why": "w", "pr_count": "three", "reviewers": "alice", "prs": "#1",
                                               "tribal": "false", "category": None}],
                                  "reviewer_profiles": [{"reviewer": "A", "focus_areas": [{"category": "security", "count": "x"}, "bad"],
                                                         "signature_concerns": "one", "quotes": ["bad", {"pr": "#1", "text": "t"}]}]})
        e, p = d["entries"][0], d["reviewer_profiles"][0]
        assert e["pr_count"] == 0 and e["reviewers"] == ["alice"] and e["prs"] == ["#1"] and e["tribal"] is False
        assert p["focus_areas"] == [{"category": "security", "count": 0}] and p["signature_concerns"] == ["one"]
        assert p["quotes"] == [{"pr": "#1", "text": "t"}]

    def test_numeric_string_counts_and_true_flags_kept(self, tmp_path):
        e = self._load(tmp_path, {"entries": [{"rule": "r", "why": "w", "pr_count": "3", "tribal": True}]})["entries"][0]
        assert e["pr_count"] == 3 and e["tribal"] is True

    def test_refined_without_entries_list_falls_back_to_raw(self, tmp_path):
        (tmp_path / "review_learnings.json").write_text('{"entries": [{"rule": "raw", "why": "w"}]}')
        (tmp_path / "review_learnings_refined.json").write_text('{"note": "oops"}')
        assert load_learnings(tmp_path)["entries"][0]["rule"] == "raw"

    def test_refined_empty_list_is_respected(self, tmp_path):
        (tmp_path / "review_learnings.json").write_text('{"entries": [{"rule": "raw", "why": "w"}]}')
        (tmp_path / "review_learnings_refined.json").write_text('{"entries": []}')
        assert load_learnings(tmp_path)["entries"] == []

    def test_top_level_not_a_dict(self, tmp_path):
        assert self._load(tmp_path, ["x"]) == {"entries": [], "reviewer_profiles": []}


class TestRestrictToRequestedReviewers:
    """With --reviewers, only what the requested people said may reach the report and the knowledge base."""

    ENTRIES = {"entries": [
        {"rule": "mine", "why": "w", "reviewers": ["Casey Jordan"]},
        {"rule": "shared", "why": "w", "reviewers": ["Casey Jordan", "Someone Else"]},
        {"rule": "theirs", "why": "w", "reviewers": ["Someone Else"]},
        {"rule": "nobody", "why": "w", "reviewers": []},
    ], "reviewer_profiles": [{"reviewer": "Casey Jordan"}, {"reviewer": "Someone Else"}]}

    def _setup(self, tmp_path, reviewers, aliases):
        from scripts.analyze.audit_options import AuditOptions, write
        (tmp_path / "review_learnings.json").write_text(json.dumps(self.ENTRIES))
        write(tmp_path, AuditOptions(reviewers=reviewers), aliases=aliases)

    def test_keeps_only_requested_people_through_their_display_names(self, tmp_path):
        self._setup(tmp_path, ("cj",), {"cj": ["Casey Jordan", "cj"]})
        d = load_learnings(tmp_path)
        assert [e["rule"] for e in d["entries"]] == ["mine", "shared"]
        assert d["entries"][1]["reviewers"] == ["Casey Jordan"]  # others are trimmed from a shared entry
        assert [p["reviewer"] for p in d["reviewer_profiles"]] == ["Casey Jordan"]

    def test_requested_but_never_matched_means_nothing_not_everything(self, tmp_path):
        self._setup(tmp_path, ("cj",), {})
        assert load_learnings(tmp_path) == {"entries": [], "reviewer_profiles": []}

    def test_no_reviewers_requested_keeps_every_entry_but_no_profiles(self, tmp_path):
        self._setup(tmp_path, (), {})
        d = load_learnings(tmp_path)
        assert len(d["entries"]) == 4 and d["reviewer_profiles"] == []
