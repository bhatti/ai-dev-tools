"""Post the standup brief to Slack and write final artifacts.

Usage:
    python -m scripts.standup.post

Optional env:
    SLACK_BOT_TOKEN          — if set, posts brief to the standup channel
    SLACK_STANDUP_CHANNEL    — channel name (default: standup)

Reads:  /workspace/standup_brief.md
        /workspace/risk_report.md    (optional)
        /workspace/synthesize_result.json
Writes: /workspace/standup_report.md   combined report artifact
        /workspace/post_result.json

Exit codes: 0=done, 1=error
"""

from __future__ import annotations

import json
import sys
from datetime import date

from scripts.common.config import load_config, get_workspace_dir
from scripts.standup.slack_client import post_message


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)

    brief_path = workspace_dir / "standup_brief.md"
    if not brief_path.exists():
        print("ERROR: standup_brief.md not found — run synthesize step first", file=sys.stderr)
        (workspace_dir / "post_result.json").write_text(
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

    # Build combined artifact
    combined_parts = [
        f"# Standup Report — {date.today().isoformat()}",
        "",
        brief,
    ]
    if risk_report:
        combined_parts += ["", "---", "", "## Full Risk Report", "", risk_report]

    combined = "\n".join(combined_parts)
    (workspace_dir / "standup_report.md").write_text(combined)
    print("[post] standup_report.md written", flush=True)

    # Post to Slack
    slack_ok = post_message(config, brief)

    post_result = {
        "status": "DONE" if slack_ok else "SLACK_FAILED",
        "slack_posted": slack_ok,
        "risk_count": synth_result.get("risk_count", 0),
        "discussion_questions": synth_result.get("discussion_questions", 0),
        "silence_count": synth_result.get("silence_count", 0),
        "issue_count": gather_result.get("issue_count", 0),
        "pr_count": gather_result.get("pr_count", 0),
        "slack_message_count": gather_result.get("slack_message_count", 0),
        "sprint": gather_result.get("sprint", ""),
        "date": date.today().isoformat(),
    }
    (workspace_dir / "post_result.json").write_text(json.dumps(post_result, indent=2))

    print(
        f"[post] done — slack_posted={slack_ok} "
        f"issues={post_result['issue_count']} prs={post_result['pr_count']} "
        f"risks={post_result['risk_count']} "
        f"questions={post_result['discussion_questions']}",
        flush=True,
    )
    # Print final JSON for formicary report_stdout capture
    print(json.dumps(post_result))
    if not slack_ok:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    from scripts.common.entrypoint import run_main
    run_main(main, "post_result.json")
