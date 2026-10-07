# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Slack emoji shortcode conversion in HTML/MD report rendering."""
from __future__ import annotations

import pytest

from scripts.common.slack_emoji import convert_slack_emoji
from scripts.common.report_renderer import render_simple_html, _inline_md_to_html


# ---------------------------------------------------------------------------
# convert_slack_emoji
# ---------------------------------------------------------------------------

def test_convert_single_shortcode():
    assert convert_slack_emoji(":rocket:") == "🚀"


def test_convert_spiral_calendar():
    assert convert_slack_emoji(":spiral_calendar_pad:") == "🗓️"


def test_convert_multiple_shortcodes():
    result = convert_slack_emoji(":rocket: Call to Action\n:bust_in_silhouette: Per-Person Status")
    assert "🚀" in result
    assert "👤" in result
    assert ":rocket:" not in result
    assert ":bust_in_silhouette:" not in result


def test_preserves_unknown_shortcode():
    # Unknown shortcodes must be left unchanged
    assert convert_slack_emoji(":nonexistent_emoji:") == ":nonexistent_emoji:"


def test_preserves_non_emoji_colons():
    # Python type hints and dicts use colons; don't mangle them
    assert convert_slack_emoji("key: value") == "key: value"
    assert convert_slack_emoji("http://example.com") == "http://example.com"


def test_convert_in_sentence():
    text = "Sprint 205 runs through :calendar: Mon Oct 12."
    result = convert_slack_emoji(text)
    assert "📅" in result
    assert ":calendar:" not in result
    assert "Sprint 205" in result


def test_all_status_indicators():
    inputs = [
        (":white_check_mark:", "✅"),
        (":x:", "❌"),
        (":warning:", "⚠️"),
        (":red_circle:", "🔴"),
        (":large_green_circle:", "🟢"),
        (":large_yellow_circle:", "🟡"),
        (":rotating_light:", "🚨"),
    ]
    for shortcode, expected in inputs:
        assert convert_slack_emoji(shortcode) == expected, f"{shortcode} not converted"


def test_report_headers():
    shortcodes = [
        ":bar_chart:", ":mag:", ":arrows_counterclockwise:",
        ":spiral_calendar_pad:", ":rocket:", ":bust_in_silhouette:",
    ]
    for sc in shortcodes:
        result = convert_slack_emoji(sc)
        assert result != sc, f"{sc} was not converted"
        assert ":" not in result or result.startswith(":") is False or len(result) > 2


# ---------------------------------------------------------------------------
# render_simple_html — shortcodes converted in output HTML
# ---------------------------------------------------------------------------

def test_render_simple_html_converts_shortcodes():
    md = "# :spiral_calendar_pad: Standup Brief\n\n:rocket: Call to Action\n\n:bust_in_silhouette: Per-Person Status"
    html = render_simple_html("Test", md)
    assert "🗓️" in html
    assert "🚀" in html
    assert "👤" in html
    assert ":spiral_calendar_pad:" not in html
    assert ":rocket:" not in html
    assert ":bust_in_silhouette:" not in html


def test_render_simple_html_converts_in_paragraphs():
    md = "Status is :white_check_mark: for all items.\n\n:warning: One issue found."
    html = render_simple_html("Test", md)
    assert "✅" in html
    assert "⚠️" in html
    assert ":white_check_mark:" not in html
    assert ":warning:" not in html


def test_render_simple_html_converts_in_headings():
    md = "## :fire: Hot Issues\n\nSome content."
    html = render_simple_html("Test", md)
    assert "🔥" in html
    assert ":fire:" not in html


def test_render_simple_html_converts_in_list_items():
    md = "- :white_check_mark: Done\n- :x: Failed\n- :warning: Warning"
    html = render_simple_html("Test", md)
    assert "✅" in html
    assert "❌" in html
    assert "⚠️" in html
    assert ":white_check_mark:" not in html


def test_inline_md_to_html_converts_shortcodes():
    result = _inline_md_to_html(":rocket: Deploy completed :white_check_mark:")
    assert "🚀" in result
    assert "✅" in result
    assert ":rocket:" not in result
    assert ":white_check_mark:" not in result


def test_render_preserves_real_emoji():
    md = "Already has ✅ real emoji and :white_check_mark: shortcode."
    html = render_simple_html("Test", md)
    assert html.count("✅") == 2  # one from real, one converted from shortcode
    assert ":white_check_mark:" not in html


# ---------------------------------------------------------------------------
# Standup-style report (simulates AI-generated content)
# ---------------------------------------------------------------------------

def test_full_standup_style_report():
    """Simulate the kind of AI-generated content that previously had broken shortcodes."""
    md = """:spiral_calendar_pad: Standup Brief — TestTeam Sprint 42 · Wed Oct 7 2026

Sprint 42 runs through Mon Oct 12. 5 days remain.

:rocket: Call to Action

All 3 open PRs need review. PR #101 has been open 14 days.

:bust_in_silhouette: Per-Person Status

- Alice: :white_check_mark: 2 PRs merged
- Bob: :warning: 1 PR stalled
- Carol: :red_circle: Build failing

:bar_chart: Metrics

| Metric | Value |
|--------|-------|
| Open PRs | 3 |
| CI Status | :large_green_circle: |
"""
    html = render_simple_html("Standup", md)
    # All shortcodes must be converted
    for shortcode in [
        ":spiral_calendar_pad:", ":rocket:", ":bust_in_silhouette:",
        ":white_check_mark:", ":warning:", ":red_circle:",
        ":bar_chart:", ":large_green_circle:",
    ]:
        assert shortcode not in html, f"{shortcode} was not converted in HTML"
    # Actual emoji must be present
    for emoji in ["🗓️", "🚀", "👤", "✅", "⚠️", "🔴", "📊", "🟢"]:
        assert emoji in html, f"{emoji} missing from HTML"
