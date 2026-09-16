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
import re
import sys
from pathlib import Path

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
    full_path = reports_dir / "audit_report.md"
    if not full_path.exists():
        full_path = workspace_dir / "audit_report.md"
    if not full_path.exists():
        print("ERROR: audit_report.md not found — run audit step first", file=sys.stderr)
        sys.exit(1)

    summary_path = reports_dir / "slack_summary.md"
    if full_report or not summary_path.exists():
        if not full_report and not summary_path.exists():
            print("[post_audit] slack_summary.md not found — falling back to full report", flush=True)
        report_text = full_path.read_text(encoding="utf-8")
    else:
        report_text = summary_path.read_text(encoding="utf-8")
        print("[post_audit] posting Slack digest (use --full for complete report)", flush=True)

    # Always read full report text for HTML attachment
    full_report_text = full_path.read_text(encoding="utf-8")

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
    html_url, job_url = build_artifact_links(config, "audit", "audit_report.html")
    artifact_link = ""
    if html_url:
        artifact_link = f"\n<{html_url}|View audit_report.html>  |  <{job_url}|All artifacts>"

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

    # Prepend consistent metadata header to HTML/MD artifact
    meta: list[str] = [f"**{commits} commits analyzed**"]
    if commit_from and commit_to:
        meta.append(f"`{commit_from}` ({commit_from_date}) → `{commit_to}` ({commit_to_date})")
    summary = f"**{critical_count} critical · {high_count} high**"
    md_header = build_md_report_header("Codebase Audit", repo or "repo", branch, meta, summary)
    body = re.sub(r"^#{1,3}\s+Codebase Audit[^\n]*\n", "", full_report_text.lstrip(), count=1)
    full_report_text = md_header + body

    # --- Format and post with HTML attachment ---
    # Slack body: digest or full depending on flag; HTML is always the full report.
    title = f"Codebase Audit — {repo or 'repo'}" + (f" @ {branch}" if branch else "")
    slack_text = format_for_slack(header + report_text)
    thread_ts = config.get("SLACK_THREAD_TS") or None
    slack_ok = post_report(config, slack_text, full_report_text,
                           title=title, filename="audit_report.html",
                           thread_ts=thread_ts, task_type="audit")

    result = {
        "status": "OK" if slack_ok else "SLACK_FAILED",
        "critical_count": critical_count,
        "high_count": high_count,
        "report_bytes": len(full_report_text),
        "slack_bytes": len(report_text),
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
