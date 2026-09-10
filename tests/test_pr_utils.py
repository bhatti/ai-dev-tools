"""Tests for scripts/common/pr_utils.py"""

import pytest

from scripts.common.pr_utils import REPLIED_TO_RE, is_bot_trigger


class TestIsBotTrigger:
    def test_ai_bot(self):
        assert is_bot_trigger("ai-bot please fix this") is True

    def test_claude_bot(self):
        assert is_bot_trigger("claude-bot add tests") is True

    def test_bare_bot(self):
        assert is_bot_trigger("bot do something") is True

    def test_not_a_bot_trigger(self):
        assert is_bot_trigger("please review this") is False

    def test_bot_in_middle(self):
        # "bot" not in first word
        assert is_bot_trigger("please ai-bot fix this") is False

    def test_empty_body(self):
        assert is_bot_trigger("") is False

    def test_whitespace_only(self):
        assert is_bot_trigger("   ") is False

    def test_case_insensitive(self):
        assert is_bot_trigger("AI-BOT fix this") is True

    def test_leading_whitespace(self):
        assert is_bot_trigger("  ai-bot fix this") is True

    def test_word_ending_bot(self):
        assert is_bot_trigger("mybot do stuff") is True


class TestRepliedToRe:
    def test_basic_match(self):
        body = "Done.\n\n<!-- replied-to: 12345 -->"
        matches = list(REPLIED_TO_RE.finditer(body))
        assert len(matches) == 1
        assert int(matches[0].group(1)) == 12345

    def test_no_spaces(self):
        body = "<!--replied-to:99-->"
        matches = list(REPLIED_TO_RE.finditer(body))
        assert len(matches) == 1
        assert int(matches[0].group(1)) == 99

    def test_extra_spaces(self):
        body = "<!--  replied-to:  42  -->"
        matches = list(REPLIED_TO_RE.finditer(body))
        assert len(matches) == 1
        assert int(matches[0].group(1)) == 42

    def test_no_match(self):
        body = "Just a regular comment"
        matches = list(REPLIED_TO_RE.finditer(body))
        assert len(matches) == 0

    def test_multiple_matches(self):
        body = "<!-- replied-to: 1 -->\n<!-- replied-to: 2 -->"
        ids = {int(m.group(1)) for m in REPLIED_TO_RE.finditer(body)}
        assert ids == {1, 2}
