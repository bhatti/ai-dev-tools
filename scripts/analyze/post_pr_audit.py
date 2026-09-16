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

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.slack_format import build_artifact_links, build_md_report_header, format_for_slack, is_full_report
from scripts.standup.slack_client import post_report


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    reports_dir = workspace_dir / "reports"

    # --- Choose Slack body: digest by default, full report when --full was passed ---
    # HTML attachment is always the full report regardless of this flag.
    full_report = is_full_report(config)
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

    # Always read full report for HTML/MD attachment; header injected below after metadata load
    full_report_text = full_path.read_text(encoding="utf-8")

    # --- Read finding counts from JSON ---
    spec_gaps = 0
    design_gaps = 0
    skill_gaps = 0
    practice_gaps = 0
    repo = ""
    branch = ""
    prs_analyzed: int | str = "?"

    date_from = ""
    date_to = ""
    jiras_reviewed = 0

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
            date_from = findings.get("date_from", "")
            date_to = findings.get("date_to", "")
            jiras_reviewed = findings.get("jiras_reviewed", 0)
        except Exception as e:
            print(f"[post-pr-audit] warning: could not parse findings JSON: {e}", flush=True)
    else:
        print("[post-pr-audit] pr_audit_findings.json not found -- counts will be 0", flush=True)

    # --- Build header summary ---
    html_url, job_url = build_artifact_links(config, "audit-prs", "pr_audit_report.html")
    artifact_link = ""
    if html_url:
        artifact_link = f"\n<{html_url}|View pr_audit_report.html>  |  <{job_url}|All artifacts>"

    date_range = ""
    if date_from and date_to:
        date_range = f"\n{date_from} → {date_to}"
    elif date_from:
        date_range = f"\n{date_from}"

    jira_info = ""
    if jiras_reviewed:
        jira_info = f" from {jiras_reviewed} Jira issues"

    header = (
        f":mag: *PR Audit* -- {repo or 'repo'}"
        + (f" @ {branch}" if branch else "")
        + f" ({prs_analyzed} PRs{jira_info})"
        + date_range
        + f"\n*{spec_gaps} spec | {design_gaps} design | {skill_gaps} skill | {practice_gaps} practice gaps*"
        + artifact_link
        + "\n\n"
    )

    # Prepend consistent metadata header to HTML/MD artifact
    meta: list[str] = [f"**{prs_analyzed} PRs analyzed**"]
    if date_from and date_to:
        meta.append(f"{date_from} → {date_to}")
    elif date_from:
        meta.append(date_from)
    if jiras_reviewed:
        meta.append(f"{jiras_reviewed} Jira issues reviewed")
    summary = f"**{spec_gaps} spec | {design_gaps} design | {skill_gaps} skill | {practice_gaps} practice gaps**"
    md_header = build_md_report_header("PR Audit", repo or "repo", branch, meta, summary)
    # Strip any leading `# PR Audit` heading Claude may have written to avoid duplication
    body = re.sub(r"^#\s+PR Audit[^\n]*\n", "", full_report_text, count=1)
    full_report_text = md_header + body

    # --- Format and post with HTML attachment ---
    # Slack body: digest or full depending on flag; HTML is always the full report.
    title = f"PR Audit — {repo or 'repo'}" + (f" @ {branch}" if branch else "")
    slack_text = format_for_slack(header + report_text)
    thread_ts = config.get("SLACK_THREAD_TS") or None
    slack_ok = post_report(config, slack_text, full_report_text,
                           title=title, filename="pr_audit_report.html",
                           thread_ts=thread_ts, task_type="audit-prs")

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
