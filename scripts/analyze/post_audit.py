"""Post codebase audit report to Slack.

Reads reports/audit_report.md and reports/audit_findings.json from WORKSPACE_DIR.
Posts formatted report to Slack with the same max-char chunking as standup/post.py.

Usage:
    python -m scripts.analyze.post_audit

Env:
    WORKSPACE_DIR           workspace directory (default: /workspace)
    SLACK_BOT_TOKEN         Slack bot token
    SLACK_CHANNEL           channel to post to
    SLACK_THREAD_TS         optional thread timestamp
    FORMICARY_PUBLIC_URL    base URL for job artifacts link
    JOB_ID                  job request ID
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.slack_format import format_for_slack
from scripts.standup.slack_client import post_message


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    reports_dir = workspace_dir / "reports"

    # --- Read audit report ---
    report_path = reports_dir / "audit_report.md"
    if not report_path.exists():
        report_path = workspace_dir / "audit_report.md"
    if not report_path.exists():
        print("ERROR: audit_report.md not found — run audit step first", file=sys.stderr)
        sys.exit(1)

    report_text = report_path.read_text(encoding="utf-8")

    # --- Read finding counts from JSON ---
    critical_count = 0
    high_count = 0
    findings_path = reports_dir / "audit_findings.json"
    if findings_path.exists():
        try:
            findings = json.loads(findings_path.read_text(encoding="utf-8"))
            critical_count = findings.get("critical_count", 0)
            high_count = findings.get("high_count", 0)
            repo = findings.get("repo", "")
            branch = findings.get("branch", "")
            commits = findings.get("commits_analyzed", "?")
            commit_from = findings.get("commit_from", "")
            commit_from_date = findings.get("commit_from_date", "")
            commit_to = findings.get("commit_to", "")
            commit_to_date = findings.get("commit_to_date", "")
        except Exception as e:
            print(f"[post_audit] warning: could not parse findings JSON: {e}", flush=True)
            repo = branch = commit_from = commit_from_date = commit_to = commit_to_date = ""
            commits = "?"
    else:
        print("[post_audit] audit_findings.json not found — counts will be 0", flush=True)
        repo = branch = commit_from = commit_from_date = commit_to = commit_to_date = ""
        commits = "?"

    # --- Build header summary ---
    formicary_url = config.get("FORMICARY_PUBLIC_URL", "").rstrip("/")
    job_id = config.get("JOB_ID", "")

    artifact_link = ""
    if formicary_url and job_id:
        artifact_link = f"\n<{formicary_url}/dashboard/jobs/requests/{job_id}|View full report & artifacts>"

    commit_range = ""
    if commit_from and commit_to:
        commit_range = f"\n`{commit_from}` ({commit_from_date}) → `{commit_to}` ({commit_to_date})"

    header = (
        f":mag: *Codebase Audit* — {repo or 'repo'}"
        + (f" @ {branch}" if branch else "")
        + f" ({commits} commits)"
        + commit_range
        + f"\n*{critical_count} critical · {high_count} high*"
        + artifact_link
        + "\n\n"
    )

    # --- Format and post (plain mrkdwn — no blocks; large audit reports exceed Slack's 50-block limit) ---
    slack_text = format_for_slack(header + report_text)
    thread_ts = config.get("SLACK_THREAD_TS", "")
    slack_ok = post_message(config, slack_text, thread_ts=thread_ts)

    result = {
        "status": "OK" if slack_ok else "SLACK_FAILED",
        "critical_count": critical_count,
        "high_count": high_count,
        "report_bytes": len(report_text),
        "slack_posted": slack_ok,
    }
    (reports_dir / "post_audit_result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(f"[post_audit] critical={critical_count} high={high_count} slack={'ok' if slack_ok else 'FAILED'}", flush=True)
    if not slack_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
