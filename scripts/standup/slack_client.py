"""Shared Slack Web API helpers used by both Jira and GitHub standup gather scripts.

Required env (optional — gracefully skipped when absent):
    SLACK_BOT_TOKEN         xoxb-... bot token
    SLACK_STANDUP_CHANNEL   channel name without '#' (default: standup)

Bot scopes needed: channels:history, channels:read, groups:history, groups:read,
                   chat:write, users:read
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta

import requests


_BLOCKER_KEYWORDS = (
    "blocked", "stuck", "waiting on", "help needed", "blocker",
    "can't proceed", "cannot proceed", "need input", "escalat",
)


def _slack_get(token: str, method: str, **params) -> dict:
    """Call a Slack GET endpoint. Returns {} on HTTP error or ok=false."""
    resp = requests.get(
        f"https://slack.com/api/{method}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20,
    )
    if not resp.ok:
        print(f"[slack] {method} HTTP {resp.status_code}", file=sys.stderr, flush=True)
        return {}
    data = resp.json()
    if not data.get("ok"):
        # Return {} so callers never see a cursor from an error body
        print(f"[slack] {method} error: {data.get('error', 'unknown')}", file=sys.stderr, flush=True)
        return {}
    return data


def resolve_channel_id(token: str, channel_name: str) -> str | None:
    name = channel_name.lstrip("#")
    cursor = None
    while True:
        params: dict = {"types": "public_channel,private_channel", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _slack_get(token, "conversations.list", **params)
        if not data:
            break   # error — stop paginating
        for ch in data.get("channels", []):
            if ch.get("name") == name:
                return ch["id"]
        cursor = data.get("response_metadata", {}).get("next_cursor") or ""
        if not cursor:
            break
    return None


def get_standup_messages(config: dict, lookback_hours: int = 26) -> list[dict]:
    """Fetch recent messages from the standup channel.

    Returns empty list (gracefully) when SLACK_BOT_TOKEN is not set or the
    channel cannot be found — Slack is optional; the brief still works without it.
    """
    token = config.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("[slack] SLACK_BOT_TOKEN not set — skipping Slack signals", flush=True)
        return []

    channel_name = config.get("SLACK_STANDUP_CHANNEL", "standup")
    channel_id = resolve_channel_id(token, channel_name)
    if not channel_id:
        print(f"[slack] channel '{channel_name}' not found — skipping", flush=True)
        return []

    oldest = str(
        (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp()
    )
    data = _slack_get(token, "conversations.history", channel=channel_id, oldest=oldest, limit=200)

    messages = []
    for m in data.get("messages", []):
        text = m.get("text", "")
        ts = m.get("ts", "")
        user = m.get("user", "")
        has_blocker = any(kw in text.lower() for kw in _BLOCKER_KEYWORDS)
        messages.append({
            "user": user,
            "text": text,
            "ts": ts,
            "has_blocker_keyword": has_blocker,
        })

    print(f"[slack] {len(messages)} messages from #{channel_name}", flush=True)
    return messages


def post_message(config: dict, text: str, channel: str | None = None) -> bool:
    """Post a message to Slack. Returns True on success, False (no exception) on failure."""
    token = config.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("[slack] SLACK_BOT_TOKEN not set — cannot post brief", flush=True)
        return False

    ch = channel or config.get("SLACK_STANDUP_CHANNEL", "standup")
    if not ch.startswith("#"):
        ch = f"#{ch}"

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"channel": ch, "text": text, "unfurl_links": False},
        timeout=20,
    )
    if not resp.ok:
        print(f"[slack] post_message HTTP {resp.status_code}", file=sys.stderr, flush=True)
        return False
    data = resp.json()
    if not data.get("ok"):
        print(f"[slack] post_message error: {data.get('error', 'unknown')}", file=sys.stderr, flush=True)
        return False
    print(f"[slack] brief posted to {ch}", flush=True)
    return True
