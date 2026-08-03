"""Post a message to Slack from env vars. Used by on_failed tasks.

Usage:
    python -m scripts.common.notify_slack "message text"
    python -m scripts.common.notify_slack  # reads MESSAGE env var

Reads from env:
    SLACK_CHANNEL, SLACK_THREAD_TS, SLACK_BOT_TOKEN
    MESSAGE (fallback if no CLI arg)
    SKILL_NAME / JOB_TYPE (included in default error message)
"""
from __future__ import annotations

import os
import sys


def main() -> None:
    sys.path.insert(0, "/app")
    from scripts.standup.slack_client import post_message

    config = dict(os.environ)
    ts = config.get("SLACK_THREAD_TS") or None

    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        skill = config.get("SKILL_NAME") or config.get("JOB_TYPE", "job")
        text = config.get("MESSAGE") or f":x: {skill} failed. Check Formicary logs for details."

    post_message(config, text, thread_ts=ts)


if __name__ == "__main__":
    main()
