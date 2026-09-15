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
import re
import sys
from pathlib import Path

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.slack_format import format_for_slack
from scripts.standup.slack_client import post_report


def _is_full_report(config: dict) -> bool:
    """Return True if --full was requested via env var (API path) or SLACK_MESSAGE (Slack path).

    run_pr_audit.py sets AUDIT_FULL_REPORT=1 in its own process, but that env var dies with
    the subprocess and is not inherited by the parent bash shell that later runs this script.
    SLACK_MESSAGE is exported by the bash routing layer and IS inherited, so check both.
    """
    if config.get("AUDIT_FULL_REPORT", "").strip() == "1":
        return True
    slack_msg = config.get("SLACK_MESSAGE", "")
    return bool(re.search(r"--full\b", slack_msg, re.IGNORECASE))


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    reports_dir = workspace_dir / "reports"

    # --- Choose Slack body: digest by default, full report when --full was passed ---
    # HTML attachment is always the full report regardless of this flag.
    full_report = _is_full_report(config)
    full_path = reports_dir / "pr_audit_report.md"
    if not full_path.exists():
        full_path = workspace_dir / "pr_audit_report.md"
    if not full_path.exists():
        print("ERROR: pr_audit_report.md not found -- run pr-audit step first", file=sys.stderr)
        sys.exit(1)

    summary_path = reports_dir / "slack_summary.md"
    if full_report or not summary_path.exists():
        if not full_report and not summary_path.exists():
            print("[post-pr-audit] slack_summary.md not found — falling back to full report", flush=True)
        report_text = full_path.read_text(encoding="utf-8")
    else:
        report_text = summary_path.read_text(encoding="utf-8")
        print("[post-pr-audit] posting Slack digest (use --full for complete report)", flush=True)

    # Always read full report text for HTML attachment
    full_report_text = full_path.read_text(encoding="utf-8")

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
        html_url = (
            f"{formicary_url}/dashboard/artifacts/by-job/{job_id}/download"
            "?file=reports/pr_audit_report.html"
        )
        artifact_link = f"\n<{html_url}|View full HTML report>"

    header = (
        f":mag: *PR Audit* -- {repo or 'repo'}"
        + (f" @ {branch}" if branch else "")
        + f" ({prs_analyzed} PRs)"
        + f"\n*{spec_gaps} spec | {design_gaps} design | {skill_gaps} skill | {practice_gaps} practice gaps*"
        + artifact_link
        + "\n\n"
    )

    # --- Format and post with HTML attachment ---
    # Slack body: digest or full depending on flag; HTML is always the full report.
    title = f"PR Audit — {repo or 'repo'}" + (f" @ {branch}" if branch else "")
    slack_text = format_for_slack(header + report_text)
    thread_ts = config.get("SLACK_THREAD_TS") or None
    slack_ok = post_report(config, slack_text, full_report_text,
                           title=title, filename="pr_audit_report.html",
                           thread_ts=thread_ts,
                           artifact_path="reports/pr_audit_report.html")

    result = {
        "status": "OK" if slack_ok else "SLACK_FAILED",
        "spec_gap_count": spec_gaps,
        "design_gap_count": design_gaps,
        "skill_gap_count": skill_gaps,
        "practice_gap_count": practice_gaps,
        "report_bytes": len(full_report_text),
        "slack_bytes": len(report_text),
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
