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
    else:
        lines += ["_No specific findings — see summary above._", ""]

    return "\n".join(l for l in lines if l is not None)


def render_report_html(findings: dict, md_text: str) -> str:
    """Wrap the Markdown report in minimal HTML for browser viewing."""
    import re
    pr_url = findings.get("pr_url", "")
    verdict = findings.get("verdict", "COMMENT")

    # Simple md → html conversion (headings, bold, italic, code, links)
    html = md_text
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$", r"<h1>\1</h1>", html, flags=re.MULTILINE)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"_\((.+?)\)_", r"<em>(\1)</em>", html)
    html = re.sub(r"`(.+?)`", r"<code>\1</code>", html)
    # Convert bare URLs to links
    if pr_url:
        html = html.replace(pr_url, f'<a href="{pr_url}">{pr_url}</a>')
    html = html.replace("\n", "<br>\n")

    title = f"PR Review — {verdict}"
    if pr_url:
        title += f" — {pr_url}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 900px; margin: 40px auto; padding: 0 20px; color: #24292e; }}
  h1 {{ border-bottom: 2px solid #e1e4e8; padding-bottom: 8px; }}
  h2 {{ border-bottom: 1px solid #e1e4e8; padding-bottom: 4px; margin-top: 24px; }}
  h3 {{ margin-top: 16px; }}
  code {{ background: #f6f8fa; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
  strong {{ font-weight: 600; }}
</style>
</head>
<body>
{html}
</body>
</html>"""


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


def _build_slack_text(findings: dict) -> str:
    """Build compact Slack mrkdwn message from findings."""
    pr_url = findings.get("pr_url", "")
    verdict = findings.get("verdict", "COMMENT")
    summary = findings.get("summary", "")
    finding_list = findings.get("findings", [])

    emoji = _VERDICT_EMOJI.get(verdict, "💬")
    lines = [
        f"{emoji} *PR Review — {verdict}*",
        f"*PR:* {pr_url}" if pr_url else "",
        "",
        summary,
    ]

    if finding_list:
        lines += ["", "*Findings:*"]
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
                lines.append(f"{sem} *{severity}*{conf_txt} — {f.get('title', '(untitled)')}{loc}")
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

    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    result_path = workspace / "post_result.json"

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
    md_path = workspace / "review_report.md"
    html_path = workspace / "review_report.html"
    md_path.write_text(md_text, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    print(f"[post_findings] wrote {md_path} ({len(md_text)} chars)", flush=True)
    print(f"[post_findings] wrote {html_path} ({len(html_text)} chars)", flush=True)

    # --- Slack post (non-fatal) ---
    token = config.get("SLACK_BOT_TOKEN", "")
    channel = config.get("SLACK_CHANNEL", "").lstrip("#")
    thread_ts = config.get("SLACK_THREAD_TS", "") or None

    if not token or not channel:
        msg = "SLACK_BOT_TOKEN or SLACK_CHANNEL not set — skipping Slack post"
        print(f"[post_findings] {msg}", flush=True)
        result_path.write_text(json.dumps({"status": "SKIPPED", "reason": msg}, indent=2), encoding="utf-8")
        sys.exit(0)

    print(f"[post_findings] posting to channel={channel} thread_ts={thread_ts}", flush=True)
    text = _build_slack_text(findings)
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
