"""Shared utilities for PR comment processing (poll-pr workflow)."""

import re

# Matches <!-- replied-to: <id> --> markers embedded in bot replies.
# Used by fetch_comments to detect already-replied comments without
# relying solely on processed_comments.json (which can be lost between iterations).
REPLIED_TO_RE = re.compile(r'<!--\s*replied-to:\s*(\d+)\s*-->')


def is_bot_trigger(body: str) -> bool:
    """Return True if the comment's first word ends with 'bot' (case-insensitive).

    Covers: ai-bot, claude-bot, mybot, etc.
    Empty or whitespace-only bodies return False.
    """
    stripped = body.strip()
    if not stripped:
        return False
    return stripped.split()[0].lower().endswith("bot")
