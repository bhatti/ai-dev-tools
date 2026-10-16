"""Tests for scripts/common/actors.py: who is a human, a review bot or a CI bot."""

from __future__ import annotations

import pytest

from scripts.common.actors import CI, HUMAN, REVIEW_BOT, classify_actor, is_ai_authored, is_bot


@pytest.mark.parametrize("login,body,kind,expected", [
    # logins that end in "bot" (or "[bot]") are bots when the account type is unknown
    ("dependabot", "", "", REVIEW_BOT), ("renovate[bot]", "", "", REVIEW_BOT), ("deploy-bot", "", "", REVIEW_BOT),
    ("my-custom-bot", "auto comment", "", REVIEW_BOT), ("Codecov[bot]", "", "", REVIEW_BOT), ("unlisted-tool[bot]", "", "", REVIEW_BOT),
    # known CI accounts, and CI-looking bodies from other bots
    ("github-actions[bot]", "", "", CI), ("jenkins", "", "", CI), ("some-bot", "Build #123 failed", "", CI),
    # review bots whose login does not end in "bot": exact logins only
    ("coderabbitai", "", "", REVIEW_BOT), ("claude[bot]", "", "", REVIEW_BOT), ("Copilot", "", "", REVIEW_BOT),
    ("copilot-pull-request-reviewer[bot]", "", "", REVIEW_BOT), ("sonarcloud", "", "", REVIEW_BOT),
    # a name that merely contains bot / claude / copilot is a person
    ("claudette", "", "", HUMAN), ("copilotfan", "", "", HUMAN), ("robotics-lead", "", "", HUMAN), ("bottle-washer", "", "", HUMAN),
    ("claude-monet", "", "", HUMAN), ("alice", "", "", HUMAN), ("", "", "", HUMAN),
    # the tracker's account type is authoritative in both directions
    ("Abbot", "", "User", HUMAN), ("talbot", "", "personal", HUMAN), ("alice", "", "Bot", REVIEW_BOT), ("anything", "", "app", REVIEW_BOT),
    ("ci-runner", "Build #7", "app_user", CI),
    ("alice", "## Code Review\nlooks fine", "User", HUMAN),
    # review-bot body markers apply only when the account type is unknown
    ("alice", "## Code Review\n...", "", REVIEW_BOT), ("alice", "ygs-review-pr output", "", REVIEW_BOT),
])
def test_classify_actor(login, body, kind, expected):
    assert classify_actor(login, body, kind) == expected


def test_is_bot_matches_classification():
    assert is_bot("dependabot") and not is_bot("Abbot", kind="User") and not is_bot("claudette")


@pytest.mark.parametrize("login,expected", [
    ("dependabot[bot]", True), ("deploybot", True), ("copilot", True), ("Copilot-swe-agent[bot]", True), ("claude-agent", True), ("ai_agent", True),
    ("claudette", False), ("copilotfan", False), ("alice", False), ("", False),
])
def test_is_ai_authored(login, expected):
    assert is_ai_authored(login) is expected
