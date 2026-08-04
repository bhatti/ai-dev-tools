"""Post review findings to Slack as a plain text message and exit 0.

Usage:
    python -m scripts.review.post_findings --findings /workspace/findings.json

Required env: SLACK_BOT_TOKEN, SLACK_CHANNEL
Optional env: SLACK_THREAD_TS

Reads:  findings.json
Writes: /workspace/post_result.json

Exit codes: 0=ok, 1=error posting (non-fatal — writes error to post_result.json)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
import requests

from scripts.common.config import get_workspace_dir, load_config

_SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🔵",
}


def _post_text(token: str, channel: str, thread_ts: str | None, text: str) -> dict:
    payload: dict = {
        "channel": channel,
        "text": text,
        "unfurl_links": False,
        "mrkdwn": True,
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


def _build_text(findings: dict) -> str:
    pr_url = findings.get("pr_url", "")
    verdict = findings.get("verdict", "COMMENT")
    summary = findings.get("summary", "")
    finding_list = findings.get("findings", [])

    verdict_emoji = {"APPROVE": "✅", "REQUEST_CHANGES": "🔄", "COMMENT": "💬"}.get(verdict, "💬")

    lines = [
        f"{verdict_emoji} *PR Review — {verdict}*",
        f"*PR:* {pr_url}" if pr_url else "",
        "",
        summary,
    ]

    if finding_list:
        lines.append("")
        lines.append("*Findings:*")
        for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            for f in finding_list:
                if f.get("severity", "").upper() != severity:
                    continue
                emoji = _SEVERITY_EMOJI.get(severity, "•")
                location = ""
                if f.get("file"):
                    location = f" — `{f['file']}`"
                    if f.get("line"):
                        location += f":{f['line']}"
                confidence = f.get("confidence", "")
                conf_text = f" _(confidence: {confidence})_" if confidence else ""
                lines.append(
                    f"{emoji} *{severity}*{conf_text} — {f.get('title', '(untitled)')}{location}"
                )
                if f.get("description"):
                    lines.append(f"  {f['description']}")
    else:
        lines.append("_No specific findings — see summary above._")

    return "\n".join(l for l in lines if l is not None)


@click.command()
@click.option("--findings", "findings_path", default="/workspace/findings.json", show_default=True,
              help="Path to findings.json written by run.py")
def main(findings_path: str) -> None:
    config = load_config()

    token = config.get("SLACK_BOT_TOKEN", "")
    channel = config.get("SLACK_CHANNEL", "").lstrip("#")
    thread_ts = config.get("SLACK_THREAD_TS", "") or None

    workspace = get_workspace_dir(config)
    result_path = workspace / "post_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)

    if not token or not channel:
        msg = "SLACK_BOT_TOKEN or SLACK_CHANNEL not set — skipping Slack post"
        print(f"[post_findings] {msg}", file=sys.stderr, flush=True)
        result_path.write_text(json.dumps({"status": "SKIPPED", "reason": msg}), encoding="utf-8")
        sys.exit(0)

    fpath = Path(findings_path)
    if not fpath.exists():
        print(f"[post_findings] findings not found at {findings_path}", file=sys.stderr, flush=True)
        findings: dict = {
            "pr_url": "", "verdict": "COMMENT", "findings": [],
            "summary": "Review artifacts not found.",
        }
    else:
        findings = json.loads(fpath.read_text(encoding="utf-8"))

    print(f"[post_findings] posting to channel={channel} thread_ts={thread_ts}", flush=True)

    text = _build_text(findings)
    response = _post_text(token, channel, thread_ts, text)

    if response.get("ok"):
        msg_ts = response.get("ts", "")
        print(f"[post_findings] posted ts={msg_ts}", flush=True)
        result_path.write_text(json.dumps({
            "status": "POSTED",
            "channel": channel,
            "ts": msg_ts,
        }, indent=2), encoding="utf-8")
    else:
        result_path.write_text(json.dumps({
            "status": "FAILED",
            "channel": channel,
            "error": response.get("error", "unknown"),
        }, indent=2), encoding="utf-8")

    sys.exit(0)


if __name__ == "__main__":
    main()
