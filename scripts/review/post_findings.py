"""Post review findings to Slack as Block Kit buttons, then PAUSE for human decision.

Usage:
    python -m scripts.review.post_findings --findings /workspace/findings.json

Required env: SLACK_BOT_TOKEN, SLACK_CHANNEL
Optional env: SLACK_THREAD_TS, JOB_ID

Reads:  findings.json
Writes: /workspace/post_result.json

Exit codes: 3=posted and waiting for signal (formicary maps to PAUSE_JOB), 1=error
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
import requests

from scripts.common.config import get_workspace_dir, load_config

# Severity → display emoji
_SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🔵",
}


def _post_block_kit(token: str, channel: str, thread_ts: str | None, blocks: list) -> dict:
    """POST chat.postMessage with Block Kit blocks. Returns parsed response."""
    payload: dict = {
        "channel": channel,
        "blocks": blocks,
        "unfurl_links": False,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    if not resp.ok:
        print(f"[post_findings] HTTP {resp.status_code}", file=sys.stderr, flush=True)
        return {}
    data = resp.json()
    if not data.get("ok"):
        print(f"[post_findings] Slack error: {data.get('error', 'unknown')}", file=sys.stderr, flush=True)
    return data


def _build_blocks(findings: dict, job_id: str) -> list:
    """Build Slack Block Kit blocks from findings dict."""
    pr_url = findings.get("pr_url", "")
    verdict = findings.get("verdict", "COMMENT")
    summary = findings.get("summary", "")
    finding_list = findings.get("findings", [])

    verdict_emoji = {"APPROVE": "✅", "REQUEST_CHANGES": "🔄", "COMMENT": "💬"}.get(verdict, "💬")

    blocks: list = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{verdict_emoji} PR Review — {verdict}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*<{pr_url}|PR Link>*\n{summary}"},
        },
    ]

    if finding_list:
        blocks.append({"type": "divider"})

        # Group by severity for better readability — CRITICAL/HIGH first
        for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            for f in finding_list:
                if f.get("severity", "").upper() != severity:
                    continue
                emoji = _SEVERITY_EMOJI.get(severity, "•")
                location = ""
                if f.get("file"):
                    location = f"\n`{f['file']}`"
                    if f.get("line"):
                        location += f":{f['line']}"
                confidence = f.get("confidence", "")
                conf_text = f" _(confidence: {confidence})_" if confidence else ""
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{emoji} *{severity}* — {f.get('title', '(untitled)')}{conf_text}"
                            f"{location}\n{f.get('description', '')}"
                        ),
                    },
                })
    else:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_No specific findings — see summary above._"},
        })

    # Action buttons — value encodes {job_id}:{decision} for the router to parse
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ Approve"},
                "style": "primary",
                "action_id": "review_decision",
                "value": f"{job_id}:approve",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔄 Request Changes"},
                "style": "danger",
                "action_id": "review_decision",
                "value": f"{job_id}:request-changes",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔍 Verify"},
                "action_id": "review_decision",
                "value": f"{job_id}:verify",
            },
        ],
    })

    return blocks


@click.command()
@click.option("--findings", "findings_path", default="/workspace/findings.json", show_default=True,
              help="Path to findings.json written by run.py")
def main(findings_path: str) -> None:
    config = load_config()

    token = config.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("[post_findings] SLACK_BOT_TOKEN not set — cannot post Block Kit", file=sys.stderr, flush=True)
        sys.exit(3)  # Still exit 3 so formicary pauses; timer will eventually release it

    channel = config.get("SLACK_CHANNEL", "")
    thread_ts = config.get("SLACK_THREAD_TS", "") or None
    job_id = config.get("JOB_ID", "unknown")

    # Resolve channel: strip leading # if present
    channel = channel.lstrip("#")
    if not channel:
        print("[post_findings] SLACK_CHANNEL not set", file=sys.stderr, flush=True)
        sys.exit(3)

    # Read findings
    fpath = Path(findings_path)
    if not fpath.exists():
        print(f"[post_findings] findings not found at {findings_path}", file=sys.stderr, flush=True)
        findings: dict = {"pr_url": "", "verdict": "COMMENT", "findings": [], "summary": "Review artifacts not found."}
    else:
        findings = json.loads(fpath.read_text(encoding="utf-8"))

    print(f"[post_findings] posting to channel={channel} thread_ts={thread_ts} job_id={job_id}", flush=True)

    blocks = _build_blocks(findings, job_id)
    response = _post_block_kit(token, channel, thread_ts, blocks)

    workspace = get_workspace_dir(config)
    result_path = workspace / "post_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)

    if response.get("ok"):
        msg_ts = response.get("ts", "")
        print(f"[post_findings] posted ts={msg_ts}", flush=True)
        result_path.write_text(json.dumps({
            "status": "POSTED",
            "channel": channel,
            "ts": msg_ts,
            "job_id": job_id,
        }, indent=2), encoding="utf-8")
    else:
        result_path.write_text(json.dumps({
            "status": "FAILED",
            "channel": channel,
            "job_id": job_id,
            "error": response.get("error", "unknown"),
        }, indent=2), encoding="utf-8")

    # Always exit 3 — formicary maps this to PAUSE_JOB.
    # The job resumes when the Slack router calls POST /api/jobs/requests/:id/trigger
    # with Decision=approve|request-changes injected as a job variable.
    sys.exit(3)


if __name__ == "__main__":
    main()
