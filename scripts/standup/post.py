"""Post the standup brief to Slack and write final artifacts.

Usage:
    python -m scripts.standup.post

Optional env:
    SLACK_BOT_TOKEN          — if set, posts brief to the standup channel
    SLACK_CHANNEL    — channel name (default: standup)

Reads:  /workspace/standup_brief.md
        /workspace/risk_report.md    (optional)
        /workspace/synthesize_result.json
Writes: /workspace/reports/report.md   combined report artifact (job artifact)
        /workspace/reports/post_result.json

Note: reports/report.html is written by render_html.py (also a job artifact).
      HTML/MD are available in the Formicary job artifacts; Slack gets plain text only.

Exit codes: 0=done, 1=error
"""

from __future__ import annotations

import json
import sys
from datetime import date

import re

from scripts.common.config import load_config, get_workspace_dir
from scripts.standup.slack_client import post_message


_SLACK_TEXT_LIMIT = 39_000  # Slack chat.postMessage text field cap is 40,000


def _format_for_slack(text: str) -> str:
    """Convert markdown to Slack mrkdwn and truncate to Slack's text limit."""
    # **bold** → *bold*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # __bold__ → remove
    text = re.sub(r'__(.+?)__', r'\1', text)
    # inline code backticks
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # markdown headings → plain (Slack uses *bold* for emphasis, not headings)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # dash bullets → •
    text = re.sub(r'^[ \t]-[ \t]+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^-[ \t]+', '• ', text, flags=re.MULTILINE)
    # arrows
    text = re.sub(r'\s*→\s*', ': ', text)
    if len(text) > _SLACK_TEXT_LIMIT:
        text = text[:_SLACK_TEXT_LIMIT] + "\n…(truncated)"
    return text


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)

    reports_dir = workspace_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    brief_path = workspace_dir / "standup_brief.md"
    fallback_paths = [
        reports_dir / "report.md",
        workspace_dir / "standup_report.md",
    ]
    if not brief_path.exists():
        for fb in fallback_paths:
            if fb.exists():
                brief_path = fb
                print(f"[post] standup_brief.md not found — using fallback {fb.name}", flush=True)
                break
        else:
            print("ERROR: standup_brief.md not found — run synthesize step first", file=sys.stderr)
            (reports_dir / "post_result.json").write_text(
                json.dumps({"status": "ERROR", "reason": "standup_brief.md not found"})
            )
            sys.exit(1)

    brief = brief_path.read_text().strip()
    risk_report_path = workspace_dir / "risk_report.md"
    risk_report = risk_report_path.read_text().strip() if risk_report_path.exists() else ""

    synth_result = {}
    synth_path = workspace_dir / "synthesize_result.json"
    if synth_path.exists():
        try:
            synth_result = json.loads(synth_path.read_text())
        except json.JSONDecodeError:
            pass

    gather_result = {}
    gather_path = workspace_dir / "gather_result.json"
    if gather_path.exists():
        try:
            gather_result = json.loads(gather_path.read_text())
        except json.JSONDecodeError:
            pass

    today = date.today().isoformat()

    # Build combined Markdown artifact (job artifact — full detail)
    combined_parts = [
        f"# Standup Report — {today}",
        "",
        brief,
    ]
    if risk_report:
        combined_parts += ["", "---", "", "## Full Risk Report", "", risk_report]

    report_text = "\n".join(combined_parts)

    # Print full report to stdout so it appears in Formicary task logs
    print("\n" + "=" * 60, flush=True)
    print(report_text, flush=True)
    print("=" * 60 + "\n", flush=True)

    (reports_dir / "report.md").write_text(report_text)
    print("[post] reports/report.md written", flush=True)

    # Build the full Slack message: brief + risk report, then convert to mrkdwn
    full_message = brief
    if risk_report:
        full_message = brief + "\n\n---\n\n" + risk_report
    slack_text = _format_for_slack(full_message)
    (reports_dir / "slack_message.txt").write_text(slack_text)
    thread_ts = config.get("SLACK_THREAD_TS") or None
    slack_ok = post_message(config, slack_text, thread_ts=thread_ts)

    post_result = {
        "status": "DONE",
        "slack_posted": slack_ok,
        "risk_count": synth_result.get("risk_count", 0),
        "discussion_questions": synth_result.get("discussion_questions", 0),
        "silence_count": synth_result.get("silence_count", 0),
        "issue_count": gather_result.get("issue_count", 0),
        "pr_count": gather_result.get("pr_count", 0),
        "slack_message_count": gather_result.get("slack_message_count", 0),
        "sprint": gather_result.get("sprint", ""),
        "date": today,
    }
    (reports_dir / "post_result.json").write_text(json.dumps(post_result, indent=2))

    print(
        f"[post] done — slack_posted={slack_ok} "
        f"issues={post_result['issue_count']} prs={post_result['pr_count']} "
        f"risks={post_result['risk_count']} "
        f"questions={post_result['discussion_questions']}",
        flush=True,
    )
    print(f"[post] result: {json.dumps(post_result)}")
    sys.exit(0)


if __name__ == "__main__":
    from scripts.common.entrypoint import run_main
    run_main(main, "reports/post_result.json")
