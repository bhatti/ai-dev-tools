"""Tests for scripts/analyze/kb_writer.py (review-knowledge checklist files, merge-only)."""

from scripts.analyze.kb_writer import (
    KB_ROOT,
    build_kb_files,
    merge_checklist,
    normalize_category,
    select_entries,
)


def _e(rule, cat="security", why="because", look="a pattern", prs=3, reviewers=(), tribal=False):
    return {"category": cat, "rule": rule, "why": why, "look_for": look, "pr_count": prs,
            "reviewers": list(reviewers), "tribal": tribal}


class TestNormalizeCategory:
    def test_aliases_and_unknown(self):
        assert normalize_category("Arch/Design") == "architecture"
        assert normalize_category("sre/ops") == "sre"
        assert normalize_category("code-style/idiom") == "style"
        assert normalize_category("api/compat") == "api-compat"
        assert normalize_category("???") == "process"


class TestSelectEntries:
    def test_threshold_priority_reviewer_and_tribal_exemptions(self):
        entries = [_e("a", prs=1), _e("b", prs=2), _e("c", prs=1, reviewers=["Senior"]), _e("d", prs=1, tribal=True)]
        kept = [e["rule"] for e in select_entries(entries, priority_reviewers=("senior",), min_prs=2)]
        assert kept == ["b", "c", "d"]

    def test_drops_entries_without_rule_or_why(self):
        assert select_entries([_e("", prs=5), _e("x", why="", prs=5)], (), 2) == []


class TestMergeChecklist:
    def test_creates_file_with_header_and_items(self):
        text, added = merge_checklist("", "security", [_e("Validate input at the boundary")])
        assert text.startswith("# Security") and added == 1
        assert "- [ ] Validate input at the boundary — Why: because — Look for: a pattern" in text

    def test_skips_duplicates_by_normalized_rule_and_keeps_existing_content(self):
        first, _ = merge_checklist("", "security", [_e("Validate input at the boundary")])
        again, added = merge_checklist(first, "security", [_e("validate  input at the boundary!"), _e("Rotate keys")])
        assert added == 1 and again.startswith(first.rstrip("\n")) and again.count("Validate input") == 1

    def test_tribal_marker_rendered(self):
        text, _ = merge_checklist("", "sre", [_e("Never hold the lock across I/O", cat="sre", tribal=True)])
        assert "(tribal)" in text

    def test_idempotent(self):
        once, _ = merge_checklist("", "testing", [_e("No sleeps", cat="testing")])
        twice, added = merge_checklist(once, "testing", [_e("No sleeps", cat="testing")])
        assert added == 0 and twice == once


class TestBuildKbFiles:
    def test_layout_index_and_reviewer_profile(self):
        entries = [_e("Validate input", cat="security", reviewers=["Alice"]), _e("Avoid N+1", cat="performance")]
        profiles = [{"reviewer": "Alice", "focus_areas": [{"category": "security", "count": 4}],
                     "signature_concerns": ["Trust boundaries"], "quotes": [{"pr": "#1", "text": "check the tenant"}]}]
        files = build_kb_files(entries, profiles, existing={})
        assert f"{KB_ROOT}/SKILL.md" in files
        assert f"{KB_ROOT}/checklists/security.md" in files and f"{KB_ROOT}/checklists/performance.md" in files
        assert "security.md" in files[f"{KB_ROOT}/SKILL.md"]
        assert "reviewed PR" in files[f"{KB_ROOT}/SKILL.md"]
        prof = files[f"{KB_ROOT}/reviewers/alice.md"]
        assert "Trust boundaries" in prof and "[security](../checklists/security.md)" in prof

    def test_existing_files_are_merged_not_replaced_and_unchanged_files_omitted(self):
        sec, _ = merge_checklist("", "security", [_e("Old rule")])
        files = build_kb_files([_e("Old rule"), _e("New rule")], [], existing={f"{KB_ROOT}/checklists/security.md": sec})
        assert "Old rule" in files[f"{KB_ROOT}/checklists/security.md"] and "New rule" in files[f"{KB_ROOT}/checklists/security.md"]
        none = build_kb_files([_e("Old rule")], [], existing={f"{KB_ROOT}/checklists/security.md": sec})
        assert f"{KB_ROOT}/checklists/security.md" not in none

    def test_no_entries_no_files(self):
        assert build_kb_files([], [], existing={}) == {}
