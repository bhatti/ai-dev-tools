"""Post the standup brief to Slack and write final artifacts.

Usage:
    python -m scripts.standup.post

Optional env:
    SLACK_BOT_TOKEN          — if set, posts brief to the standup channel
    SLACK_CHANNEL    — channel name (default: standup)

Reads:  /workspace/standup_brief.md
        /workspace/signals.json      (optional, for board status block)
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

from scripts.common.config import load_config, get_workspace_dir
from scripts.common.slack_format import format_for_slack
from scripts.standup.render_html import DONE_STATUSES
from scripts.standup.slack_client import post_report


def _board_status_md(signals: dict) -> str:
    """Generate a compact Markdown board-status table from signals."""
    from datetime import date, datetime
    sprints = signals.get("all_sprints", [])
    issues = signals.get("issues", [])
    if not sprints:
        return ""

    total = len(issues)
    done_n = sum(1 for i in issues if i.get("status", "").lower() in DONE_STATUSES)
    active = sum(1 for i in issues if i.get("status", "").lower() in ("in progress", "in review", "review"))
    not_started = total - done_n - active
    today = date.today()

    rows = []
    seen: set = set()
    for s in sprints:
        sid = s.get("id")
        if sid in seen:
            continue
        seen.add(sid)
        end_str = s.get("end_date", "")
        days_left = "?"
        if end_str:
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                days_left = str((end_dt.date() - today).days)
            except ValueError:
                pass
        rows.append(f"| {s.get('board','—')} | {s.get('name','—')} | {total} | {done_n} | {active} | {not_started} | {days_left} |")

    if not rows:
        return ""
    header = "| Board | Sprint | Total | Done | Active | Not Started | Days Left |"
    sep = "|-------|--------|-------|------|--------|-------------|-----------|"
    return "\n".join([header, sep] + rows)


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

    # Board status block for MD (mirrors HTML top section)
    board_md = ""
    signals_path = workspace_dir / "signals.json"
    if signals_path.exists():
        try:
            board_md = _board_status_md(json.loads(signals_path.read_text()))
        except Exception:
            pass

    # Build combined Markdown artifact (job artifact — full detail)
    combined_parts = [f"# Standup Report — {today}", ""]
    if board_md:
        combined_parts += ["## Board Status", "", board_md, ""]
    combined_parts += [brief]
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
    slack_text = format_for_slack(full_message)
    (reports_dir / "slack_message.txt").write_text(slack_text)
    thread_ts = config.get("SLACK_THREAD_TS") or None
    slack_ok = post_report(config, slack_text, report_text,
                           title="Daily Standup", filename="report.html",
                           thread_ts=thread_ts)

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
