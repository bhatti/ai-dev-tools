"""Tests for scripts/common/text_utils.py."""

from scripts.common.text_utils import slug, strip_ansi


def test_strip_ansi_removes_colour_and_cursor_sequences_only():
    assert strip_ansi("\x1b[31mred\x1b[39m \x1b[2K\x1b[1Gplain [31m kept") == "red plain [31m kept"


def test_slug_is_unchanged():
    assert slug("Fix: the BIG bug!") == "fix-the-big-bug"
