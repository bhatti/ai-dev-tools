"""Shared Slack Web API helpers used by both Jira and GitHub standup gather scripts.

Required env (optional — gracefully skipped when absent):
    SLACK_BOT_TOKEN         xoxb-... bot token
    SLACK_CHANNEL   channel name without '#' (default: standup)

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

    channel_name = config.get("SLACK_CHANNEL", "standup")
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


def upload_file(config: dict, file_path: str, filename: str, channel: str | None = None,
                initial_comment: str = "") -> bool:
    """Upload a file to Slack using the v2 upload API (getUploadURLExternal flow).

    Requires the files:write scope.
    Returns True on success, False (no exception) on any failure.
    Silently skips when SLACK_BOT_TOKEN is absent.
    """
    token = config.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("[slack] SLACK_BOT_TOKEN not set — cannot upload file", flush=True)
        return False

    import os
    file_size = os.path.getsize(file_path)

    # Step 1: request an upload URL
    resp = requests.post(
        "https://slack.com/api/files.getUploadURLExternal",
        headers={"Authorization": f"Bearer {token}"},
        data={"filename": filename, "length": file_size},
        timeout=20,
    )
    if not resp.ok:
        print(f"[slack] getUploadURLExternal HTTP {resp.status_code}", file=sys.stderr, flush=True)
        return False
    data = resp.json()
    if not data.get("ok"):
        print(f"[slack] getUploadURLExternal error: {data.get('error', 'unknown')}", file=sys.stderr, flush=True)
        return False
    upload_url = data["upload_url"]
    file_id = data["file_id"]

    # Step 2: PUT the file content to the upload URL
    with open(file_path, "rb") as fh:
        put_resp = requests.put(upload_url, data=fh, timeout=60)
    if not put_resp.ok:
        print(f"[slack] file PUT HTTP {put_resp.status_code}", file=sys.stderr, flush=True)
        return False

    # Step 3: complete the upload and share to channel
    ch = channel or config.get("SLACK_CHANNEL", "standup")
    ch = ch.lstrip("#")
    channel_id = resolve_channel_id(token, ch)
    if not channel_id:
        print(f"[slack] channel '{ch}' not found — cannot complete upload", flush=True)
        return False

    complete_payload = {
        "files": [{"id": file_id}],
        "channel_id": channel_id,
    }
    if initial_comment:
        complete_payload["initial_comment"] = initial_comment

    complete_resp = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=complete_payload,
        timeout=20,
    )
    if not complete_resp.ok:
        print(f"[slack] completeUploadExternal HTTP {complete_resp.status_code}", file=sys.stderr, flush=True)
        return False
    result = complete_resp.json()
    if not result.get("ok"):
        print(f"[slack] completeUploadExternal error: {result.get('error', 'unknown')}", file=sys.stderr, flush=True)
        return False

    print(f"[slack] file '{filename}' uploaded to {ch}", flush=True)
    return True


def post_message(config: dict, text: str, channel: str | None = None,
                 thread_ts: str | None = None) -> bool:
    """Post a message to Slack. Returns True on success, False (no exception) on failure.

    If thread_ts is provided (or SLACK_THREAD_TS is set in config), the message
    is posted as a thread reply instead of to the channel root.
    """
    token = config.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("[slack] SLACK_BOT_TOKEN not set — cannot post brief", flush=True)
        return False

    ch = channel or config.get("SLACK_CHANNEL", "standup")
    # Slack's chat.postMessage accepts channel IDs (C0ABC123) and names (general) directly.
    # Do NOT prepend '#' — channel IDs with '#' are invalid and cause channel_not_found.
    ch = ch.lstrip("#")

    payload: dict = {"channel": ch, "text": text, "unfurl_links": False, "mrkdwn": True}
    ts = thread_ts or config.get("SLACK_THREAD_TS", "") or None
    if ts:
        payload["thread_ts"] = ts

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    if not resp.ok:
        print(f"[slack] post_message HTTP {resp.status_code}", file=sys.stderr, flush=True)
        return False
    data = resp.json()
    if not data.get("ok"):
        print(f"[slack] post_message error: {data.get('error', 'unknown')}", file=sys.stderr, flush=True)
        return False
    dest = f"{ch} (thread)" if ts else ch
    print(f"[slack] brief posted to {dest}", flush=True)
    return True


def notify(config: dict, text: str, channel_key: str = "SLACK_CHANNEL") -> bool:
    """Post a notification to Slack using the channel from config[channel_key].

    Optional — returns True on success, False (logged, no exception) on any failure.
    Silently skips when SLACK_BOT_TOKEN or the channel env var is absent.
    """
    token = config.get("SLACK_BOT_TOKEN", "")
    if not token:
        print(f"[slack] SLACK_BOT_TOKEN not set — skipping notification", flush=True)
        return False
    channel = config.get(channel_key, "")
    if not channel:
        print(f"[slack] {channel_key} not set — skipping notification", flush=True)
        return False
    return post_message(config, text, channel=channel)


if __name__ == "__main__":
    import os
    import sys
    _config = dict(os.environ)
    _ts = _config.get("SLACK_THREAD_TS") or None
    _skill = _config.get("SKILL_NAME") or _config.get("JOB_TYPE", "job")
    _text = _config.get("MESSAGE") or (sys.argv[1] if len(sys.argv) > 1 else f":x: {_skill} failed. Check Formicary logs.")
    post_message(_config, _text, thread_ts=_ts)
