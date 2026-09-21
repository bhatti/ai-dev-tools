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
                initial_comment: str = "", thread_ts: str = "") -> bool:
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
        err = data.get("error", "unknown")
        if err == "missing_scope":
            print(
                "[slack] file upload failed: Slack bot token missing 'files:write' scope. "
                "Add it at api.slack.com/apps → OAuth & Permissions → Bot Token Scopes → files:write, "
                "then reinstall the app to your workspace.",
                file=sys.stderr, flush=True,
            )
        else:
            print(f"[slack] getUploadURLExternal error: {err}", file=sys.stderr, flush=True)
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
    if thread_ts:
        complete_payload["thread_ts"] = thread_ts

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


_PR_PRIORITY_EMOJI = {"blocker": "🚨", "critical": "🔴", "high": "🟠"}
_PR_CI_EMOJI = {"success": "✅", "failure": "❌", "pending": "⏳", "none": ""}
_PR_GROUP_BADGE = {
    "CI FAILING": "🔴",
    "READY TO MERGE": "✅",
    "APPROVED — WAITING ON CI": "⏳",
    "APPROVED (1 review)": "👍",
    "STALE / AT RISK (>5d)": "⚠️",
    "NEEDS REVIEW (>1d)": "🔔",
    "IN REVIEW": "👀",
}
_PR_GROUP_ORDER = [
    "CI FAILING",
    "READY TO MERGE",
    "APPROVED — WAITING ON CI",
    "APPROVED (1 review)",
    "STALE / AT RISK (>5d)",
    "NEEDS REVIEW (>1d)",
    "IN REVIEW",
]


def _pr_group(pr: dict) -> str:
    """Classify a PR dict into one of the _PR_GROUP_ORDER groups."""
    ci = pr.get("ci_status", "none")
    # approval_count preferred; fall back to len(approved_by) for backward compat
    n = pr.get("approval_count")
    if n is None:
        n = len(pr.get("approved_by") or [])
    days = pr.get("age_days", 0)
    if ci == "failure":
        return "CI FAILING"
    if n >= 2 and ci in ("success", "none"):
        return "READY TO MERGE"
    if n >= 2 and ci == "pending":
        return "APPROVED — WAITING ON CI"
    if n >= 1:
        return "APPROVED (1 review)"
    if days > 5:
        return "STALE / AT RISK (>5d)"
    if days > 1:
        return "NEEDS REVIEW (>1d)"
    return "IN REVIEW"


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

    grouped: dict[str, list] = {g: [] for g in _PR_GROUP_ORDER}
    for pr in prs:
        grouped[_pr_group(pr)].append(pr)

    for group_name in _PR_GROUP_ORDER:
        group_prs = grouped[group_name]
        if not group_prs:
            continue
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": group_name, "emoji": True},
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
            ci_icon = _PR_CI_EMOJI.get(pr.get("ci_status", "none"), "")

            # Build clickable links
            jira_link = f"<{jira_url}|{jira_key}>" if jira_url and jira_key else jira_key
            pr_link = f"<{pr_url}|PR #{pr_num}>" if pr_url and pr_num else f"PR #{pr_num}"

            priority = (pr.get("priority") or "").strip()
            priority_emoji = _PR_PRIORITY_EMOJI.get(priority.lower(), "")
            labels = [l for l in (pr.get("labels") or []) if isinstance(l, str) and l and len(l) <= 25][:3]

            reviewer_info = ""
            if approved_by:
                reviewer_info += f"approved-by: {', '.join('@' + n.split()[0] for n in approved_by[:3])}"
            if pending:
                if reviewer_info:
                    reviewer_info += "  "
                reviewer_info += f"pending: {', '.join('@' + n.split()[0] for n in pending[:4])}"
            if not reviewer_info:
                reviewer_info = "no reviewers"
            if priority:
                reviewer_info += f"  •  P: {priority}"
            if labels:
                reviewer_info += f"  •  {' '.join(f'`{l}`' for l in labels)}"

            ci_prefix = f"{ci_icon} " if ci_icon else ""
            line = f"{ci_prefix}{priority_emoji}{jira_link}  {pr_link}  @{author} ({days}d)  {title_text}"
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
        raw_labels = issue.get("labels") or []
        labels = [lbl if isinstance(lbl, str) else lbl.get("name", "") for lbl in raw_labels]
        label_str = ", ".join(l for l in labels[:3] if l) or "—"
        priority = (issue.get("priority") or "—")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{url}|#{number}> *{issue_title}*"},
            "fields": [
                {"type": "mrkdwn", "text": f"*Assignee:* {assignee}   *Priority:* {priority}"},
                {"type": "mrkdwn", "text": f"*Labels:* {label_str}"},
            ],
        })
    blocks.append({"type": "divider"})
    return blocks


def _post_message_ts(config: dict, text: str, channel: str | None = None,
                     thread_ts: str | None = None,
                     blocks: list | None = None) -> str | None:
    """Send chat.postMessage and return the message ts on success, None on failure."""
    token = config.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("[slack] SLACK_BOT_TOKEN not set — cannot post message", flush=True)
        return None

    ch = channel or config.get("SLACK_CHANNEL", "")
    if not ch:
        print("[slack] no channel set — skipping post_message", flush=True)
        return None
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
        return None
    data = resp.json()
    if not data.get("ok"):
        print(f"[slack] post_message error: {data.get('error', 'unknown')}", file=sys.stderr, flush=True)
        return None
    dest = f"{ch} (thread)" if ts else ch
    print(f"[slack] message posted to {dest}", flush=True)
    return data.get("ts", "")


def post_message(config: dict, text: str, channel: str | None = None,
                 thread_ts: str | None = None,
                 blocks: list | None = None) -> bool:
    """Post a message to Slack. Returns True on success, False (no exception) on failure."""
    return _post_message_ts(config, text, channel=channel,
                            thread_ts=thread_ts, blocks=blocks) is not None


def _upload_html_to_formicary(config: dict, html: str, filename: str) -> str | None:
    """Upload HTML content directly to the formicary artifact store.

    Returns the direct download URL (dashboard/artifacts/{sha256}/download) or None on failure.
    Used as a fallback when Slack file upload is unavailable.
    """
    public_url = (config.get("FORMICARY_PUBLIC_URL") or config.get("FORMICARY_URL") or "").rstrip("/")
    token = config.get("FORMICARY_TOKEN", "")
    if not public_url or not token or not html:
        return None
    try:
        # formicary's artifact API reads all request headers as metadata params;
        # "name" sets the artifact filename. Content-Type is preserved as-is.
        # verify=False: formicary nip.io deployments use self-signed certs.
        resp = requests.post(
            f"{public_url}/api/artifacts",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "text/html",
                "name": filename,
            },
            data=html.encode("utf-8"),
            timeout=30,
            verify=False,
        )
        if resp.ok:
            sha256 = resp.json().get("sha256", "")
            if sha256:
                url = f"{public_url}/dashboard/artifacts/{sha256}/download"
                print(f"[slack] uploaded '{filename}' to formicary → {url}", flush=True)
                return url
        print(f"[slack] formicary artifact upload HTTP {resp.status_code}", flush=True)
    except Exception as e:
        print(f"[slack] formicary artifact upload error: {e}", flush=True)
    return None


def upload_html_report(config: dict, html_content: str, filename: str,
                       thread_ts: str | None, task_type: str = "run",
                       channel: str | None = None) -> bool:
    """Upload HTML to Slack as a file; post an artifact fallback link on failure.

    Called after the main Slack message is already posted.  `thread_ts` is the
    ts to reply to (the original thread or the new message's ts).

    Primary path: upload `html_content` as a Slack file (requires files:write scope).
    Fallback when upload fails: post a direct formicary download link, or a by-job
    artifact endpoint link as last resort.

    Returns True if the HTML was uploaded or a fallback link was posted.
    """
    import os
    import tempfile
    from scripts.common.slack_format import build_artifact_links

    if not html_content:
        return False

    tmp_path: str | None = None
    upload_ok = False
    try:
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w",
                                        encoding="utf-8") as fh:
            tmp_path = fh.name  # set before write so finally can unlink on IOError
            fh.write(html_content)
        upload_ok = upload_file(config, tmp_path, filename,
                                channel=channel, thread_ts=thread_ts)
        if not upload_ok:
            print(f"[slack] WARNING: HTML upload failed for '{filename}' — "
                  f"check bot has files:write scope and is in the channel", flush=True)
    except Exception as e:
        print(f"[slack] HTML upload error for '{filename}' (non-fatal): {e}", flush=True)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if not upload_ok:
        direct_url = _upload_html_to_formicary(config, html_content, filename)
        by_job_url, job_link = build_artifact_links(config, task_type, filename)
        if direct_url and job_link:
            fallback_text = f"📎 Full report: <{direct_url}|{filename}>  |  <{job_link}|All artifacts>"
        elif by_job_url:
            fallback_text = f"📎 Full report: <{by_job_url}|View {filename}>  |  <{job_link}|All artifacts>"
        else:
            fallback_text = None
        if fallback_text:
            _post_message_ts(config, fallback_text, channel=channel, thread_ts=thread_ts)
        return bool(fallback_text)

    return True


def post_report(config: dict, slack_text: str, md_text: str,
                title: str, filename: str,
                thread_ts: str | None = None,
                channel: str | None = None,
                task_type: str = "post") -> bool:
    """Post a mrkdwn report to Slack and upload an HTML version in the same thread.

    Primary path: render HTML and upload as a Slack file (requires files:write scope).
    Fallback when Slack upload fails: upload HTML directly to formicary artifact store
    and post a direct download link.

    Args:
        slack_text:  Pre-formatted mrkdwn text (from format_for_slack).
        md_text:     Original markdown source used to render the HTML.
        title:       HTML page <title> and <h1>.
        filename:    Slack display name for the uploaded file (e.g. "audit_report.html").
        thread_ts:   Existing thread to reply into.  When None the text message creates a
                     new top-level post and the HTML is threaded to that new message's ts.
        task_type:   Formicary task type that produces the report artifact (e.g. "audit-prs").
                     Used in the fallback link to select the correct task's artifact zip.
    """
    from scripts.common.report_renderer import render_simple_html

    msg_ts = _post_message_ts(config, slack_text, channel=channel, thread_ts=thread_ts)
    if not msg_ts:
        return False

    html: str = ""
    try:
        html = render_simple_html(title, md_text)
    except Exception as e:
        print(f"[slack] HTML render error for '{filename}' (non-fatal): {e}", flush=True)

    if html:
        upload_html_report(config, html, filename, thread_ts=thread_ts or msg_ts,
                           task_type=task_type, channel=channel)
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
