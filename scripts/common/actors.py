"""Who wrote a PR comment, review or task: human, code-review bot or CI bot.

One definition for every PR report (audit, merge queue, open PRs, PR risk, PR review). Trackers that expose an
account type pass it as ``kind`` (GitHub GraphQL ``__typename`` ``Bot``, Bitbucket ``account_type`` ``app``);
name and body heuristics cover everything else.
"""

from __future__ import annotations

import re

# AI coding-agent PR authors that do not end in "bot": matched as whole name tokens, never as a substring
# ("claudette" and "copilotfan" are people).
AI_AGENT_AUTHOR_RE = re.compile(r"(?:^|[-_.])(?:copilot|claude[-_]agent|ai[-_]agent)(?:$|[-_.\[])", re.IGNORECASE)

# CI bots: build runners, test runners, status reporters — NOT code reviewers
CI_BOT_USERNAMES: frozenset[str] = frozenset({
    "github-actions[bot]", "circleci", "jenkins", "travis-ci",
    "buildkite", "teamcity", "azure-pipelines[bot]", "circle-ci[bot]",
})
CI_BOT_BODY_PATTERNS: tuple[str, ...] = (
    "Linux - ", "Windows - ", "macOS - ", "Build #",
    "GitHub Actions", "CircleCI", "Jenkins build", "Travis CI",
)

# Code-review bots whose login does not end in "bot": exact logins (case-insensitive, "[bot]" optional). A substring
# would also match people ("claudette"), so these are never matched inside a longer name.
CODE_REVIEW_BOT_LOGINS: frozenset[str] = frozenset({
    "claude", "copilot", "copilot-pull-request-reviewer", "coderabbitai", "codeclimate", "sonarcloud", "codacy-production",
    "snyk-bot", "deepsource-autofix", "reviewdog", "github-advanced-security",
})
CODE_REVIEW_BOT_BODY_MARKERS: tuple[str, ...] = (
    "ygs-review-pr", "ygs-security-review", "## Code Review",
    "## Security Review", "## Audit Report", "pr-review-agent",
)

# Account types the trackers report (compared case-insensitively). An explicit type is authoritative in both directions:
# a "User" named Abbot is a person, an "app" named anything is a bot.
_BOT_KINDS = frozenset({"bot", "app", "app_user"})
_HUMAN_KINDS = frozenset({"user", "personal", "team", "organization", "mannequin"})

CI = "ci"
REVIEW_BOT = "review_bot"
HUMAN = "human"


def classify_actor(login: str, body: str = "", kind: str = "") -> str:
    """Return ``"ci"``, ``"review_bot"`` or ``"human"``.

    Order: known CI logins; the tracker's account ``kind`` when it has one (authoritative: a human account is never a
    bot because of its name or text); exact known review-bot logins; a login that *ends* in ``bot`` (``dependabot``,
    ``deploy-bot``, ``x[bot]``); review-bot body markers (only when the account type is unknown). A name that merely
    contains "bot", "claude" or "copilot" is just a name.
    """
    lower = (login or "").strip().lower()
    body = body or ""
    kind = (kind or "").strip().lower()
    if lower in CI_BOT_USERNAMES:
        return CI
    if kind in _HUMAN_KINDS:
        return HUMAN
    base = lower[:-5] if lower.endswith("[bot]") else lower
    if kind in _BOT_KINDS or base in CODE_REVIEW_BOT_LOGINS or lower.endswith("[bot]") or base.endswith("bot"):
        return CI if any(pattern in body for pattern in CI_BOT_BODY_PATTERNS) else REVIEW_BOT
    if not kind and any(marker in body for marker in CODE_REVIEW_BOT_BODY_MARKERS):
        return REVIEW_BOT
    return HUMAN


def is_bot(login: str, body: str = "", kind: str = "") -> bool:
    """True for any bot (CI or review bot)."""
    return classify_actor(login, body, kind) != HUMAN


def is_ai_authored(login: str) -> bool:
    """True if a PR author is an AI/bot agent: the login ends in ``bot``/``[bot]`` or is a known coding agent."""
    if not login:
        return False
    lower = login.lower()
    base = lower[:-5] if lower.endswith("[bot]") else lower
    return base.endswith("bot") or bool(AI_AGENT_AUTHOR_RE.search(lower))
