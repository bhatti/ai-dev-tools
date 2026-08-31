"""Shared Slack Web API helpers used by both Jira and GitHub standup gather scripts.

Required env (optional — gracefully skipped when absent):
    SLACK_BOT_TOKEN         xoxb-... bot token
    SLACK_CHANNEL   channel name without '#' (default: standup)

Bot scopes needed: channels:history, channels:read, groups:history, groups:read,
                   chat:write, users:read
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone, timedelta

import requests


_BLOCKER_KEYWORDS = (
    "blocked", "stuck", "waiting on", "help needed", "blocker",
    "can't proceed", "cannot proceed", "need input", "escalat",
)

_channel_id_cache: dict[tuple, str] = {}


def _slack_get(token: str, method: str, **params) -> dict:
    """Call a Slack GET endpoint. Returns {} on HTTP error or ok=false.

    Retries up to 3 times with exponential backoff (1s, 2s, 4s) on HTTP 429.
    """
    delays = [1, 2, 4]
    for attempt, delay in enumerate(delays + [0]):
        resp = requests.get(
            f"https://slack.com/api/{method}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=20,
        )
        if resp.status_code == 429:
            if attempt < len(delays):
                wait = min(delay, 5)  # cap at 5s regardless of Retry-After
                print(f"[slack] {method} HTTP 429 — retrying in {wait}s (attempt {attempt + 1}/3)", file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            print(f"[slack] {method} HTTP 429 — max retries exceeded", file=sys.stderr, flush=True)
            return {}
        if not resp.ok:
            print(f"[slack] {method} HTTP {resp.status_code}", file=sys.stderr, flush=True)
            return {}
        data = resp.json()
        if not data.get("ok"):
            # Return {} so callers never see a cursor from an error body
            print(f"[slack] {method} error: {data.get('error', 'unknown')}", file=sys.stderr, flush=True)
            return {}
        return data
    return {}


def resolve_channel_id(token: str, channel_name: str) -> str | None:
    name = channel_name.lstrip("#")
    cache_key = (token[-8:], name)
    if cache_key in _channel_id_cache:
        return _channel_id_cache[cache_key]
    cursor = None
    while True:
        params: dict = {"types": "public_channel,private_channel", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = _slack_get(token, "conversations.list", **params)
        if not data:
            break   # error — stop paginating
        for ch in data.get("channels", []):
            if ch.get("name") == name:
                _channel_id_cache[cache_key] = ch["id"]
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

    channel_name = config.get("SLACK_CHANNEL", "")
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
    ch = channel or config.get("SLACK_CHANNEL", "")
    if not ch:
        print("[slack] no channel set — skipping file upload", flush=True)
        return False
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


def build_mrkdwn_blocks(text: str, max_chars: int = 2900) -> list:
    """Wrap plain mrkdwn text in Block Kit section blocks.

    Slack section text is capped at 3000 chars.  Split on blank lines so each
    paragraph becomes its own section block — this produces a readable table-like
    layout for standup / risk-scan output that already uses mrkdwn formatting.
    """
    # Split on double newlines (paragraph breaks) to keep sections under 3000 chars
    paragraphs: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "":
            if current:
                paragraphs.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append("\n".join(current))

    blocks: list = []
    chunk: list[str] = []
    chunk_len = 0
    for para in paragraphs:
        para_len = len(para) + 2  # +2 for \n\n separator
        if chunk_len + para_len > max_chars and chunk:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n\n".join(chunk)},
            })
            chunk = []
            chunk_len = 0
        chunk.append(para)
        chunk_len += para_len
    if chunk:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n\n".join(chunk)},
        })
    blocks.append({"type": "divider"})
    return blocks


def build_pr_blocks(title: str, pr_data: dict) -> list:
    """Build Block Kit blocks from a pr_queue.json dict.

    Each PR becomes a section block with a Jira link + PR link, author, age,
    status, and reviewer info.  Groups are shown as header blocks.
    """
    prs: list[dict] = pr_data.get("prs", [])
    sprint = pr_data.get("sprint", "")
    header_text = title or f"PR Queue — {sprint}" if sprint else "PR Queue"

    blocks: list = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text[:150], "emoji": True}},
        {"type": "divider"},
    ]
    if not prs:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "_No open PRs found._"}})
        return blocks

    # Group PRs: approved, needs review, stale, in review
    def _group(pr: dict) -> str:
        days = pr.get("age_days", 0)
        approved = pr.get("approved_by") or []
        if approved and days <= 5:
            return "APPROVED — READY TO MERGE"
        if days > 5 and not approved:
            return "STALE / AT RISK (>5d, no approvals)"
        if not approved and days > 1:
            return "NEEDS REVIEW (>1d, no approvals)"
        return "IN REVIEW"

    group_order = [
        "APPROVED — READY TO MERGE",
        "NEEDS REVIEW (>1d, no approvals)",
        "STALE / AT RISK (>5d, no approvals)",
        "IN REVIEW",
    ]
    grouped: dict[str, list] = {g: [] for g in group_order}
    for pr in prs:
        grouped[_group(pr)].append(pr)

    for group_name in group_order:
        group_prs = grouped[group_name]
        if not group_prs:
            continue
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": group_name, "emoji": False},
        })
        for pr in group_prs:
            jira_key = pr.get("jira_key", "")
            jira_url = pr.get("jira_url", "")
            pr_url = pr.get("url", "")
            pr_num = pr_url.rstrip("/").split("/")[-1] if pr_url else pr.get("id", "")
            title_text = (pr.get("jira_summary") or pr.get("title") or "(no title)")[:60]
            author = (pr.get("author") or "?").split()[0]
            days = pr.get("age_days", 0)
            approved_by = pr.get("approved_by") or []
            pending = pr.get("reviewers") or []

            # Build clickable links
            jira_link = f"<{jira_url}|{jira_key}>" if jira_url and jira_key else jira_key
            pr_link = f"<{pr_url}|PR #{pr_num}>" if pr_url and pr_num else f"PR #{pr_num}"

            reviewer_info = ""
            if approved_by:
                reviewer_info += f"approved-by: {', '.join('@' + n.split()[0] for n in approved_by[:3])}"
            if pending:
                if reviewer_info:
                    reviewer_info += "  "
                reviewer_info += f"pending: {', '.join('@' + n.split()[0] for n in pending[:4])}"
            if not reviewer_info:
                reviewer_info = "no reviewers"

            line = f"{jira_link}  {pr_link}  @{author} ({days}d)  {title_text}"
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": line},
                "fields": [
                    {"type": "mrkdwn", "text": reviewer_info},
                ],
            })
    blocks.append({"type": "divider"})
    return blocks


def build_issue_blocks(title: str, issues: list, base_url: str) -> list:
    """Build Slack Block Kit blocks for a list of Jira issues.

    Each issue gets a section block with a clickable link + metadata fields.
    Pass the result as the `blocks` argument to post_message() / notify().
    `text` in post_message should be a plain-text fallback for notifications.
    """
    blocks: list = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}},
        {"type": "divider"},
    ]
    for issue in issues:
        key = issue.get("key", "?")
        fields = issue.get("fields", {})
        summary = (fields.get("summary") or "(no title)")[:80]
        status = (fields.get("status") or {}).get("name", "?")
        issuetype = (fields.get("issuetype") or {}).get("name", "")
        assignee = (fields.get("assignee") or {}).get("displayName") or "Unassigned"
        priority = (fields.get("priority") or {}).get("name") or "—"
        created = (fields.get("created") or "")[:10] or "—"
        url = f"{base_url.rstrip('/')}/browse/{key}"

        type_tag = f"[{issuetype}] " if issuetype else ""
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{url}|{key}> {type_tag}*{summary}*"},
            "fields": [
                {"type": "mrkdwn", "text": f"*Status:* {status}   *Priority:* {priority}"},
                {"type": "mrkdwn", "text": f"*Assignee:* {assignee}   *Date:* {created}"},
            ],
        })
    blocks.append({"type": "divider"})
    return blocks


def build_gh_issue_blocks(title: str, issues: list) -> list:
    """Build Slack Block Kit blocks for a list of GitHub issues."""
    blocks: list = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}},
        {"type": "divider"},
    ]
    for issue in issues:
        number = issue.get("number", "?")
        issue_title = (issue.get("title") or "(no title)")[:80]
        url = issue.get("url", "")
        assignees = issue.get("assignees") or []
        assignee = assignees[0].get("login", "Unassigned") if assignees else "Unassigned"
        labels = [lbl["name"] for lbl in (issue.get("labels") or [])]
        label_str = ", ".join(labels[:3]) or "—"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{url}|#{number}> *{issue_title}*"},
            "fields": [
                {"type": "mrkdwn", "text": f"*Assignee:* {assignee}"},
                {"type": "mrkdwn", "text": f"*Labels:* {label_str}"},
            ],
        })
    blocks.append({"type": "divider"})
    return blocks


def post_message(config: dict, text: str, channel: str | None = None,
                 thread_ts: str | None = None,
                 blocks: list | None = None) -> bool:
    """Post a message to Slack. Returns True on success, False (no exception) on failure.

    If thread_ts is provided (or SLACK_THREAD_TS is set in config), the message
    is posted as a thread reply instead of to the channel root.

    If blocks is provided, it is sent as the Block Kit payload alongside text (used as
    the fallback for notifications). When blocks is None, the message is plain mrkdwn.
    """
    token = config.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("[slack] SLACK_BOT_TOKEN not set — cannot post brief", flush=True)
        return False

    ch = channel or config.get("SLACK_CHANNEL", "")
    if not ch:
        print("[slack] no channel set — skipping post_message", flush=True)
        return False
    # Slack's chat.postMessage accepts channel IDs (C0ABC123) and names (general) directly.
    # Do NOT prepend '#' — channel IDs with '#' are invalid and cause channel_not_found.
    ch = ch.lstrip("#")

    payload: dict = {"channel": ch, "text": text, "unfurl_links": False, "mrkdwn": True}
    ts = thread_ts or config.get("SLACK_THREAD_TS", "") or None
    if ts:
        payload["thread_ts"] = ts
    if blocks:
        payload["blocks"] = blocks

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


def notify(config: dict, text: str, channel_key: str = "SLACK_CHANNEL",
           blocks: list | None = None) -> bool:
    """Post a notification to Slack using the channel from config[channel_key].

    Replies in the originating thread when SlackThreadTs is present in config.
    Optional — returns True on success, False (logged, no exception) on any failure.
    Silently skips when SLACK_BOT_TOKEN or the channel env var is absent.

    Pass blocks for structured Block Kit output (text is used as fallback).
    """
    token = config.get("SLACK_BOT_TOKEN", "")
    if not token:
        print(f"[slack] SLACK_BOT_TOKEN not set — skipping notification", flush=True)
        return False
    channel = config.get(channel_key, "")
    if not channel:
        print(f"[slack] {channel_key} not set — skipping notification", flush=True)
        return False
    thread_ts = config.get("SlackThreadTs") or config.get("SLACK_THREAD_TS") or None
    return post_message(config, text, channel=channel, thread_ts=thread_ts, blocks=blocks)


if __name__ == "__main__":
    import os
    import sys
    _config = dict(os.environ)
    _channel = _config.get("SLACK_CHANNEL", "")
    if not _channel:
        print("[slack] SLACK_CHANNEL not set — skipping notification", flush=True)
        sys.exit(0)
    _ts = _config.get("SLACK_THREAD_TS") or None
    _skill = _config.get("SKILL_NAME") or _config.get("JOB_TYPE", "job")
    _text = _config.get("MESSAGE") or (sys.argv[1] if len(sys.argv) > 1 else f":x: {_skill} failed. Check Formicary logs.")
    _public_url = (_config.get("FORMICARY_PUBLIC_URL", "") or "").rstrip("/")
    _job_id = _config.get("JOB_ID", "") or ""
    if _public_url and _job_id:
        _text += f"\n<{_public_url}/dashboard/jobs/requests/{_job_id}|View job in Formicary>"
    post_message(_config, _text, thread_ts=_ts)
