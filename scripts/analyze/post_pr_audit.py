"""Post PR audit report to Slack.

Reads reports/pr_audit_report.md and reports/pr_audit_findings.json from WORKSPACE_DIR.
Posts formatted report to Slack with the same max-char chunking as post_audit.py.

Usage:
    python -m scripts.analyze.post_pr_audit

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
from scripts.common.slack_format import format_for_slack, SLACK_TEXT_LIMIT
from scripts.standup.slack_client import post_message


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    reports_dir = workspace_dir / "reports"

    # --- Read PR audit report ---
    report_path = reports_dir / "pr_audit_report.md"
    if not report_path.exists():
        report_path = workspace_dir / "pr_audit_report.md"
    if not report_path.exists():
        print("ERROR: pr_audit_report.md not found -- run pr-audit step first", file=sys.stderr)
        sys.exit(1)

    report_text = report_path.read_text(encoding="utf-8")

    # --- Read finding counts from JSON ---
    spec_gaps = 0
    design_gaps = 0
    skill_gaps = 0
    practice_gaps = 0
    repo = ""
    branch = ""
    prs_analyzed: int | str = "?"

    findings_path = reports_dir / "pr_audit_findings.json"
    if findings_path.exists():
        try:
            findings = json.loads(findings_path.read_text(encoding="utf-8"))
            spec_gaps = findings.get("spec_gap_count", 0)
            design_gaps = findings.get("design_gap_count", 0)
            skill_gaps = findings.get("skill_gap_count", 0)
            practice_gaps = findings.get("practice_gap_count", 0)
            repo = findings.get("repo", "")
            branch = findings.get("branch", "")
            prs_analyzed = findings.get("prs_analyzed", "?")
        except Exception as e:
            print(f"[post-pr-audit] warning: could not parse findings JSON: {e}", flush=True)
    else:
        print("[post-pr-audit] pr_audit_findings.json not found -- counts will be 0", flush=True)

    # --- Build header summary ---
    formicary_url = config.get("FORMICARY_PUBLIC_URL", "").rstrip("/")
    job_id = config.get("JOB_ID", "")

    artifact_link = ""
    if formicary_url and job_id:
        artifact_link = f"\n<{formicary_url}/dashboard/jobs/requests/{job_id}|View full report & artifacts>"

    header = (
        f":mag: *PR Audit* -- {repo or 'repo'}"
        + (f" @ {branch}" if branch else "")
        + f" ({prs_analyzed} PRs)"
        + f"\n*{spec_gaps} spec | {design_gaps} design | {skill_gaps} skill | {practice_gaps} practice gaps*"
        + artifact_link
        + "\n\n"
    )

    # --- Format and post ---
    slack_text = format_for_slack(header + report_text)
    thread_ts = config.get("SLACK_THREAD_TS", "")
    slack_ok = post_message(config, slack_text, thread_ts=thread_ts)

    result = {
        "status": "OK" if slack_ok else "SLACK_FAILED",
        "spec_gap_count": spec_gaps,
        "design_gap_count": design_gaps,
        "skill_gap_count": skill_gaps,
        "practice_gap_count": practice_gaps,
        "report_bytes": len(report_text),
        "slack_posted": slack_ok,
    }
    (reports_dir / "post_pr_audit_result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8",
    )
    print(
        f"[post-pr-audit] spec={spec_gaps} design={design_gaps} skill={skill_gaps} "
        f"practice={practice_gaps} slack={'ok' if slack_ok else 'FAILED'}",
        flush=True,
    )
    if not slack_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
