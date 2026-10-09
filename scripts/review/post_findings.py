"""Post review findings to Slack and write review_report.md / review_report.html.

Usage:
    python -m scripts.review.post_findings --findings /workspace/findings.json

Required env: SLACK_BOT_TOKEN, SLACK_CHANNEL
Optional env: SLACK_THREAD_TS

Reads:  findings.json
Writes: /workspace/review_report.md   (always — human-readable Markdown report)
        /workspace/review_report.html  (always — same report as HTML)
        /workspace/post_result.json    (always — Slack post outcome)

Exit codes: 0=ok (Slack errors are non-fatal — written to post_result.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import requests

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.design_metrics import render_design_metrics_table
from scripts.common.report_renderer import render_simple_html
from scripts.common.report_utils import write_report
from scripts.common.slack_format import build_artifact_links
from scripts.standup.slack_client import upload_file as _slack_upload_file

_SEVERITY_EMOJI = {
    "CRITICAL": "🔴",
    "HIGH": "🟠",
    "MEDIUM": "🟡",
    "LOW": "🔵",
}

_VERDICT_EMOJI = {"APPROVE": "✅", "REQUEST_CHANGES": "🔄", "COMMENT": "💬"}


def render_report_md(findings: dict) -> str:
    """Render findings as a full Markdown report suitable for artifacts."""
    pr_url = findings.get("pr_url", "")
    verdict = findings.get("verdict", "COMMENT")
    summary = findings.get("summary", "")
    finding_list = findings.get("findings", [])

    emoji = _VERDICT_EMOJI.get(verdict, "💬")
    lines = [
        f"# {emoji} PR Review — {verdict}",
        "",
        f"**PR:** {pr_url}" if pr_url else "",
        "",
        f"## Summary",
        "",
        summary,
        "",
    ]

    # Design Quality Metrics table — emitted before findings when present
    lines += render_design_metrics_table(findings.get("design_metrics", {}))

    if finding_list:
        lines += ["## Findings", ""]
        for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            for f in finding_list:
                if f.get("severity", "").upper() != severity:
                    continue
                sem = _SEVERITY_EMOJI.get(severity, "•")
                loc = ""
                if f.get("file"):
                    loc = f" — `{f['file']}`"
                    if f.get("line"):
                        loc += f":{f['line']}"
                conf = f.get("confidence", "")
                conf_txt = f" _(confidence: {conf})_" if conf else ""
                lines.append(f"### {sem} {severity}{conf_txt} — {f.get('title', '(untitled)')}{loc}")
                if f.get("description"):
                    lines.append(f"")
                    lines.append(f.get("description", ""))
                if f.get("fix"):
                    lines.append(f"")
                    lines.append(f"**Fix:** {f['fix']}")
                lines.append("")
    elif not findings.get("design_metrics"):
        # Only show "no findings" when there's also no design metrics content
        lines += ["_No specific findings — see summary above._", ""]

    return "\n".join(l for l in lines if l is not None)


def render_report_html(findings: dict, md_text: str) -> str:
    """Render the Markdown PR review report as a Bootstrap HTML page."""
    verdict = findings.get("verdict", "COMMENT")
    pr_url = findings.get("pr_url", "")
    title = f"PR Review — {verdict}"
    if pr_url:
        title += f" — {pr_url}"
    return render_simple_html(title, md_text)


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


_SLACK_DESC_MAX = 220


def _build_slack_text(findings: dict) -> str:
    """Build compact Slack mrkdwn message from findings."""
    pr_url = findings.get("pr_url", "")
    verdict = findings.get("verdict", "COMMENT")
    summary = findings.get("summary", "")
    finding_list = findings.get("findings", [])

    emoji = _VERDICT_EMOJI.get(verdict, "💬")
    lines: list[str] = [f"{emoji} *PR Review — {verdict}*"]
    if pr_url:
        lines.append(f"*PR:* {pr_url}")
    if summary:
        lines += ["", summary]

    if finding_list:
        lines += ["", "*Findings:*"]
        prev_severity = None
        for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            sev_findings = [f for f in finding_list if f.get("severity", "").upper() == severity]
            if not sev_findings:
                continue
            if prev_severity is not None:
                lines.append("")
            prev_severity = severity
            for f in sev_findings:
                sem = _SEVERITY_EMOJI.get(severity, "•")
                loc = ""
                if f.get("file"):
                    loc = f" — `{f['file']}`"
                    if f.get("line"):
                        loc += f":{f['line']}"
                conf = f.get("confidence", "")
                conf_txt = f" _(confidence: {conf})_" if conf else ""
                lines.append(f"{sem} *{severity}*{conf_txt} — {f.get('title', '(untitled)')}{loc}")
                desc = (f.get("description") or "").strip()
                if desc:
                    if len(desc) > _SLACK_DESC_MAX:
                        desc = desc[:_SLACK_DESC_MAX].rstrip() + "…"
                    lines.append(f"> {desc}")
    else:
        lines.append("_No specific findings — see summary above._")

    return "\n".join(lines)


@click.command()
@click.option("--findings", "findings_path", default="reports/findings.json", show_default=True,
              help="Path to findings.json written by run.py")
def main(findings_path: str) -> None:
    config = load_config()

    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    result_path = reports_dir / "post_result.json"

    # --- Load findings (always required for report rendering) ---
    fpath = Path(findings_path)
    if not fpath.exists():
        print(f"[post_findings] findings not found at {findings_path} — using stub", file=sys.stderr, flush=True)
        findings: dict = {
            "pr_url": "", "verdict": "COMMENT", "findings": [],
            "summary": "Review artifacts not found.",
        }
    else:
        findings = json.loads(fpath.read_text(encoding="utf-8"))

    # --- Always render Markdown + HTML reports regardless of Slack ---
    md_text = render_report_md(findings)
    html_text = render_report_html(findings, md_text)
    md_path = reports_dir / "report.md"
    html_path = reports_dir / "report.html"
    if write_report(md_path, md_text):
        print(f"[post_findings] wrote {md_path} ({len(md_text)} chars)", flush=True)
    if write_report(html_path, html_text):
        print(f"[post_findings] wrote {html_path} ({len(html_text)} chars)", flush=True)

    # Upload HTML to Slack if credentials are available (non-fatal)
    token = config.get("SLACK_BOT_TOKEN", "")
    channel = config.get("SLACK_CHANNEL", "").lstrip("#")
    thread_ts = config.get("SLACK_THREAD_TS", "") or None
    if token and channel:
        try:
            uploaded = _slack_upload_file(
                config, str(html_path), "pr_review_report.html",
                channel=channel, thread_ts=thread_ts or "",
            )
            if not uploaded:
                print("[post_findings] HTML upload skipped — check channel permissions", flush=True)
        except Exception as e:
            print(f"[post_findings] HTML upload error (non-fatal): {e}", flush=True)

    # Always write slack message to artifact (regardless of whether Slack is configured)
    text = _build_slack_text(findings)
    reports_url, _ = build_artifact_links(config)
    if reports_url:
        text += f"\n📎 <{reports_url}|View reports>"
    (reports_dir / "slack_message.txt").write_text(text)

    # --- Slack post (non-fatal) ---
    if not token or not channel:
        msg = "SLACK_BOT_TOKEN or SLACK_CHANNEL not set — skipping Slack post"
        print(f"[post_findings] {msg}", flush=True)
        result_path.write_text(json.dumps({"status": "SKIPPED", "reason": msg}, indent=2), encoding="utf-8")
        sys.exit(0)
    print(f"[post_findings] posting to channel={channel} thread_ts={thread_ts}", flush=True)
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
        err = response.get("error", "unknown")
        print(f"[post_findings] Slack post failed ({err}) — report still written to artifacts", flush=True)
        result_path.write_text(json.dumps({
            "status": "FAILED",
            "channel": channel,
            "error": err,
        }, indent=2), encoding="utf-8")

    sys.exit(0)


if __name__ == "__main__":
    main()
