"""Tests for scripts/common/adf.py: Atlassian Document Format reading and section-preserving writes."""

from __future__ import annotations

import copy
import json

import pytest

from scripts.common import adf

ORIGINAL = {"type": "doc", "version": 1, "content": [
    {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Why"}]},
    {"type": "paragraph", "content": [{"type": "text", "text": "Users lose work on timeout."},
                                       {"type": "hardBreak"}, {"type": "text", "text": "See "},
                                       {"type": "inlineCard", "attrs": {"url": "https://example.com/doc"}}]},
    {"type": "bulletList", "content": [
        {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "retry on 429"}]}]},
        {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "keep the draft"}]}]}]},
    {"type": "table", "content": [{"type": "tableRow", "content": [
        {"type": "tableHeader", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "A"}]}]},
        {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "B"}]}]}]}]},
]}


class TestToText:
    def test_headings_lists_links_and_tables_survive_flattening(self):
        text = adf.to_text(ORIGINAL)
        assert "## Why" in text
        assert "Users lose work on timeout." in text and "https://example.com/doc" in text
        assert "- retry on 429" in text and "- keep the draft" in text
        assert "A | B" in text

    def test_heading_levels_are_kept(self):
        doc = {"type": "doc", "content": [{"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Deep"}]}]}
        assert "### Deep" in adf.to_text(doc)

    def test_none_and_non_documents_are_empty(self):
        assert adf.to_text(None) == "" and adf.to_text({}) == "" and adf.to_text(42) == ""

    def test_ordered_lists_code_blocks_and_rules(self):
        doc = {"type": "doc", "content": [
            {"type": "orderedList", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]}]},
            {"type": "codeBlock", "content": [{"type": "text", "text": "make test"}]},
            {"type": "rule"}]}
        text = adf.to_text(doc)
        assert "1. one" in text and "make test" in text and "---" in text


class TestFromMarkdown:
    def test_headings_bullets_numbers_paragraphs_code_and_rules(self):
        nodes = adf.from_markdown("## Acceptance Criteria (generated)\nIntro line\n\n- first\n- second\n\n1. a\n2. b\n\n```\nmake test\n```\n---\n")
        types = [n["type"] for n in nodes]
        assert types == ["heading", "paragraph", "bulletList", "orderedList", "codeBlock", "rule"]
        assert nodes[0]["attrs"]["level"] == 2
        assert [i["content"][0]["content"][0]["text"] for i in nodes[2]["content"]] == ["first", "second"]

    def test_round_trip_through_text(self):
        md = "## Plan\n- step one\n- step two\n"
        assert adf.to_text({"type": "doc", "content": adf.from_markdown(md)}).strip() == md.strip()

    def test_empty_input_gives_no_nodes(self):
        assert adf.from_markdown("  \n\n") == []


class TestSections:
    """A generated section is one panel node holding a heading and its content: its boundary is a node, never inferred."""

    HEADING = "Acceptance Criteria (generated)"

    def _new(self, heading=HEADING, body="- one"):
        return adf.from_markdown(f"## {heading}\n{body}\n")

    def test_append_adds_one_panel_and_leaves_existing_nodes_identical(self):
        before = copy.deepcopy(ORIGINAL)
        out = adf.append_section(ORIGINAL, self.HEADING, self._new())
        assert ORIGINAL == before  # input untouched
        assert out["content"][:len(before["content"])] == before["content"]
        assert len(out["content"]) == len(before["content"]) + 1 and out["content"][-1]["type"] == "panel"
        assert adf.has_section(out, self.HEADING)

    def test_a_missing_description_becomes_a_new_document(self):
        out = adf.append_section(None, self.HEADING, self._new())
        assert out["type"] == "doc" and out["version"] == 1 and adf.has_section(out, self.HEADING)

    def test_has_section_is_case_insensitive_and_ignores_other_headings(self):
        out = adf.append_section(ORIGINAL, self.HEADING, self._new())
        assert adf.has_section(out, "acceptance criteria (GENERATED)") and not adf.has_section(out, "Implementation Plan (generated)")

    def test_a_hand_written_heading_is_a_section_too_but_cannot_be_replaced(self):
        doc = adf.append_section(None, "x", [])  # an empty panel is not a section
        assert not adf.has_section(doc, self.HEADING)
        hand = {"type": "doc", "version": 1, "content": self._new()}
        assert adf.has_section(hand, self.HEADING)
        with pytest.raises(adf.NotReplaceable):
            adf.replace_section(hand, self.HEADING, self._new())

    def test_replace_swaps_exactly_the_panel_and_nothing_around_it(self):
        doc = adf.append_section(ORIGINAL, self.HEADING, self._new(body="- old"))
        doc["content"].append({"type": "paragraph", "content": [{"type": "text", "text": "USER EDIT after the section"}]})
        doc["content"].append({"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "user sub heading"}]})
        doc = adf.append_section(doc, "Implementation Plan (generated)", self._new("Implementation Plan (generated)", "- plan step"))
        out = adf.replace_section(doc, self.HEADING, self._new(body="- new"))
        text = adf.to_text(out)
        assert "- new" in text and "- old" not in text and "- plan step" in text
        assert "USER EDIT after the section" in text and "user sub heading" in text
        assert out["content"][:len(ORIGINAL["content"])] == ORIGINAL["content"] and text.count(self.HEADING) == 1

    def test_generated_content_with_its_own_headings_and_rules_cannot_confuse_the_boundary(self):
        body = "- risk one\n\n## Risks\n- r1\n\n---\nafter the rule"
        doc = adf.append_section(ORIGINAL, self.HEADING, self._new(body=body))
        out = adf.replace_section(doc, self.HEADING, self._new(body="- clean"))
        text = adf.to_text(out)
        assert "- clean" in text and "r1" not in text and "after the rule" not in text and text.count("## Risks") == 0

    def test_replace_of_a_missing_section_raises(self):
        with pytest.raises(KeyError):
            adf.replace_section(ORIGINAL, "Nope (generated)", self._new())

    def test_split_text_separates_the_section_from_everything_else(self):
        doc = adf.append_section(ORIGINAL, self.HEADING, self._new(body="- inside"))
        outside, inside = adf.split_text(doc, self.HEADING)
        assert "inside" in inside and "inside" not in outside and adf.to_text(ORIGINAL).strip() == outside.strip()
        assert adf.split_text(ORIGINAL, self.HEADING) == (adf.to_text(ORIGINAL), None)

    def test_documents_stay_json_serialisable(self):
        json.dumps(adf.append_section(ORIGINAL, self.HEADING, self._new()))


class TestNothingIsDropped:
    """A description is the spec: every node type that carries words must reach the grader."""

    def _doc(self, *nodes):
        return {"type": "doc", "version": 1, "content": list(nodes)}

    def _p(self, *inline):
        return {"type": "paragraph", "content": list(inline)}

    def test_link_urls_are_kept_next_to_their_text(self):
        link = {"type": "text", "text": "the design doc", "marks": [{"type": "link", "attrs": {"href": "https://example.com/design"}}]}
        assert "the design doc (https://example.com/design)" in adf.to_text(self._doc(self._p(link)))

    def test_inline_atoms_emoji_status_date_and_cards(self):
        text = adf.to_text(self._doc(self._p(
            {"type": "text", "text": "State "}, {"type": "status", "attrs": {"text": "IN REVIEW", "color": "blue"}},
            {"type": "text", "text": " due "}, {"type": "date", "attrs": {"timestamp": "1767225600000"}},
            {"type": "emoji", "attrs": {"shortName": ":warning:", "text": "⚠️"}},
            {"type": "inlineCard", "attrs": {"url": "https://example.com/a"}})))
        assert "IN REVIEW" in text and "2026-01-01" in text and "⚠️" in text and "https://example.com/a" in text

    def test_block_cards_media_and_panels(self):
        text = adf.to_text(self._doc(
            {"type": "blockCard", "attrs": {"url": "https://example.com/spec"}},
            {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"type": "file", "alt": "login screen.png", "id": "x"}}]},
            {"type": "panel", "attrs": {"panelType": "warning"}, "content": [self._p({"type": "text", "text": "Do not change the schema"})]},
            {"type": "expand", "attrs": {"title": "Details"}, "content": [self._p({"type": "text", "text": "hidden spec text"})]}))
        assert "https://example.com/spec" in text and "login screen.png" in text
        assert "Do not change the schema" in text and "Details" in text and "hidden spec text" in text

    def test_task_nodes_get_the_local_ids_jira_requires(self):
        task = adf.from_markdown("- [ ] one\n- [x] two\n")[0]
        ids = [task["attrs"]["localId"], *(i["attrs"]["localId"] for i in task["content"])]
        assert all(ids) and len(set(ids)) == 3

    def test_task_lists_keep_their_state(self):
        text = adf.to_text(self._doc({"type": "taskList", "content": [
            {"type": "taskItem", "attrs": {"state": "DONE"}, "content": [{"type": "text", "text": "migrate data"}]},
            {"type": "taskItem", "attrs": {"state": "TODO"}, "content": [{"type": "text", "text": "update docs"}]}]}))
        assert "- [x] migrate data" in text and "- [ ] update docs" in text

    def test_nested_lists_keep_every_item_and_their_nesting(self):
        text = adf.to_text(self._doc({"type": "bulletList", "content": [{"type": "listItem", "content": [
            self._p({"type": "text", "text": "outer"}),
            {"type": "bulletList", "content": [{"type": "listItem", "content": [self._p({"type": "text", "text": "inner"})]}]}]}]}))
        assert "- outer" in text and "inner" in text and text.index("outer") < text.index("inner")

    def test_unknown_nodes_still_yield_their_children_text(self):
        assert "inside" in adf.to_text(self._doc({"type": "someFutureNode", "content": [self._p({"type": "text", "text": "inside"})]}))

    def test_a_long_description_is_returned_whole(self):
        paragraphs = [self._p({"type": "text", "text": f"requirement {i} " + "x" * 200}) for i in range(2000)]
        text = adf.to_text(self._doc(*paragraphs))
        assert "requirement 0 " in text and "requirement 1999 " in text and len(text) > 400_000

    def test_deeply_nested_content_is_not_cut_at_a_shallow_depth(self):
        node = {"type": "paragraph", "content": [{"type": "text", "text": "deep"}]}
        for _ in range(15):
            node = {"type": "blockquote", "content": [node]}
        assert "deep" in adf.to_text(self._doc(node))


class TestMoreNodesAndReading:
    def _doc(self, *nodes):
        return {"type": "doc", "version": 1, "content": list(nodes)}

    def _p(self, *inline):
        return {"type": "paragraph", "content": list(inline)}

    def test_plain_strings_are_returned_not_emptied(self):
        assert adf.to_text("raw text from a v2 style field") == "raw text from a v2 style field"

    def test_media_inline_extensions_and_card_data(self):
        text = adf.to_text(self._doc(self._p(
            {"type": "mediaInline", "attrs": {"id": "abc", "type": "file"}},
            {"type": "inlineExtension", "attrs": {"text": "Roadmap macro", "extensionKey": "roadmap", "parameters": {"macroParams": {"title": {"value": "Q4"}}}}},
            {"type": "inlineCard", "attrs": {"data": {"name": "Design spec", "url": "https://example.com/spec"}}},
            {"type": "mention", "attrs": {"id": "user-1"}})))
        assert "[attachment: abc]" in text and "Roadmap macro" in text and "Q4" in text
        assert "https://example.com/spec" in text and "[mention: user-1]" in text

    def test_decisions_captions_quotes_and_numbering(self):
        text = adf.to_text(self._doc(
            {"type": "decisionList", "content": [
                {"type": "decisionItem", "attrs": {"state": "DECIDED"}, "content": [{"type": "text", "text": "use X"}]},
                {"type": "decisionItem", "attrs": {"state": "UNDECIDED"}, "content": [{"type": "text", "text": "pick Y"}]}]},
            {"type": "blockquote", "content": [self._p({"type": "text", "text": "quoted words"})]},
            {"type": "orderedList", "attrs": {"order": 5}, "content": [{"type": "listItem", "content": [self._p({"type": "text", "text": "fifth"})]}]}))
        assert "- [decided] use X\n" in text and "- [open] pick Y\n" in text
        assert "> quoted words" in text and "5. fifth" in text

    def test_multi_paragraph_table_cells_stay_on_one_row(self):
        cell = {"type": "tableCell", "content": [self._p({"type": "text", "text": "line one"}), self._p({"type": "text", "text": "line two"})]}
        other = {"type": "tableCell", "content": [self._p({"type": "text", "text": "B"})]}
        text = adf.to_text(self._doc({"type": "table", "content": [{"type": "tableRow", "content": [cell, other]}]}))
        assert "line one line two | B" in text

    def test_content_below_the_old_depth_guard_is_still_read(self):
        node = {"type": "paragraph", "content": [{"type": "text", "text": "very deep"}]}
        for _ in range(150):
            node = {"type": "blockquote", "content": [node]}
        assert "very deep" in adf.to_text(self._doc(node))


class TestFromMarkdownKeepsFormatting:
    def test_inline_marks_become_marks_not_literal_characters(self):
        nodes = adf.from_markdown("- **Given** a `cart`, see [the spec](https://example.com/s) and *this*\n")
        text_nodes = nodes[0]["content"][0]["content"][0]["content"]
        marks = {t["text"]: [m["type"] for m in t.get("marks", [])] for t in text_nodes}
        assert marks["Given"] == ["strong"] and marks["cart"] == ["code"] and marks["the spec"] == ["link"] and marks["this"] == ["em"]
        assert "**" not in adf.to_text({"type": "doc", "content": nodes}) and "`" not in adf.to_text({"type": "doc", "content": nodes})

    def test_nested_bullets_stay_inside_their_parent_item(self):
        nodes = adf.from_markdown("- outer\n  - inner one\n  - inner two\n- second\n")
        assert len(nodes) == 1 and nodes[0]["type"] == "bulletList" and len(nodes[0]["content"]) == 2
        inner = nodes[0]["content"][0]["content"][1]
        assert inner["type"] == "bulletList" and len(inner["content"]) == 2

    def test_plus_bullets_task_items_and_quotes(self):
        nodes = adf.from_markdown("+ plus item\n\n- [ ] todo\n- [x] done\n\n> a quote\n")
        assert [n["type"] for n in nodes] == ["bulletList", "taskList", "blockquote"]
        assert [i["attrs"]["state"] for i in nodes[1]["content"]] == ["TODO", "DONE"]

    def test_tables_become_a_code_block_that_keeps_every_cell(self):
        nodes = adf.from_markdown("| Env | Mode |\n| --- | --- |\n| single | one process |\n")
        assert [n["type"] for n in nodes] == ["codeBlock"] and "one process" in nodes[0]["content"][0]["text"]

    def test_underscores_in_identifiers_are_not_emphasis(self):
        text = adf.to_text({"type": "doc", "content": adf.from_markdown("see user_name_field and snake_case_value\n")})
        assert "user_name_field" in text and "snake_case_value" in text


class TestRobustness:
    def _doc(self, *nodes):
        return {"type": "doc", "version": 1, "content": list(nodes)}

    def test_pathological_nesting_gives_an_explicit_marker_never_an_exception(self):
        node = {"type": "paragraph", "content": [{"type": "text", "text": "buried"}]}
        for _ in range(400):
            node = {"type": "blockquote", "content": [node]}
        assert "[content nested too deep to read]" in adf.to_text(self._doc(node))

    def test_deeply_nested_lists_render_in_linear_time(self):
        item = {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "leaf"}]}]}
        node = {"type": "bulletList", "content": [item]}
        for _ in range(60):
            node = {"type": "bulletList", "content": [{"type": "listItem", "content": [node]}]}
        # exponential rendering would never finish for 60 levels; the assertion is that this returns and keeps the text
        assert "leaf" in adf.to_text(self._doc(node))

    def test_a_heading_without_a_level_is_still_read(self):
        assert "no level" in adf.to_text(self._doc({"type": "heading", "attrs": {"level": None}, "content": [{"type": "text", "text": "no level"}]}))

    def test_a_task_list_with_a_nested_bullet_is_valid_adf(self):
        nodes = adf.from_markdown("- [ ] a\n  - sub bullet\n- [x] b\n")
        assert [n["type"] for n in nodes] == ["taskList", "bulletList", "taskList"]
        assert all(i["type"] == "taskItem" and i["attrs"]["state"] in ("TODO", "DONE") for n in nodes if n["type"] == "taskList" for i in n["content"])

    def test_a_task_under_a_bullet_starts_its_own_list(self):
        nodes = adf.from_markdown("- bullet\n  - [ ] task\n")
        assert [n["type"] for n in nodes] == ["bulletList", "taskList"]


class TestOnlyOurOwnPanelsAreReplaced:
    HEADING = "Acceptance Criteria (generated)"

    def _person_panel(self, text="HUMAN EDITS"):
        return {"type": "panel", "attrs": {"panelType": "info"}, "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": self.HEADING}]},
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}

    def test_a_panel_a_person_made_with_the_same_heading_is_never_replaced(self):
        doc = {"type": "doc", "version": 1, "content": [self._person_panel()]}
        assert adf.has_section(doc, self.HEADING) and not adf.replaceable(doc, self.HEADING)
        with pytest.raises(adf.NotReplaceable):
            adf.replace_section(doc, self.HEADING, adf.from_markdown(f"## {self.HEADING}\n- new"))

    def test_our_panel_carries_a_visible_provenance_footer_and_is_replaceable(self):
        doc = adf.append_section(None, self.HEADING, adf.from_markdown(f"## {self.HEADING}\n- one"))
        assert adf.FOOTER in adf.to_text(doc) and adf.replaceable(doc, self.HEADING)

    def test_a_section_a_person_moved_into_an_expand_is_found_so_nothing_is_duplicated(self):
        expand = {"type": "expand", "attrs": {"title": "Details"}, "content": adf.from_markdown(f"## {self.HEADING}\n- moved")}
        doc = {"type": "doc", "version": 1, "content": [expand]}
        assert adf.has_section(doc, self.HEADING) and not adf.replaceable(doc, self.HEADING)
