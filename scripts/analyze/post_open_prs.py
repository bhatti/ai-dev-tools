"""Post Open PR Dashboard report to Slack.

Reads reports/open_prs_report.md and reports/open_prs_summary.json from WORKSPACE_DIR.
Posts formatted report to Slack with an HTML artifact link.

Usage:
    python -m scripts.analyze.post_open_prs

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

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.slack_format import (
    build_artifact_links,
    build_md_report_header,
    format_for_slack,
)
from scripts.standup.slack_client import post_report


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    reports_dir = workspace_dir / "reports"

    report_path = reports_dir / "open_prs_report.md"
    if not report_path.exists():
        report_path = workspace_dir / "open_prs_report.md"
    if not report_path.exists():
        print("ERROR: open_prs_report.md not found — run open-prs step first", file=sys.stderr)
        sys.exit(1)

    report_text = report_path.read_text(encoding="utf-8")

    # Read summary JSON
    total_open = 0
    high_risk = 0
    stale_count = 0
    repo = ""
    tracker = ""
    as_of = ""

    summary_path = reports_dir / "open_prs_summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            total_open = summary.get("total_open", 0)
            high_risk = summary.get("high_risk", 0)
            stale_count = summary.get("stale_count", 0)
            repo = summary.get("repo", "")
            tracker = summary.get("tracker", "")
            as_of = summary.get("as_of", "")
        except Exception as e:
            print(f"[post-open-prs] warning: could not parse summary JSON: {e}", flush=True)

    reports_url, _ = build_artifact_links(config)
    artifact_link = f"\n📎 <{reports_url}|View reports>" if reports_url else ""

    risk_summary = ""
    if high_risk:
        risk_summary = f" — *{high_risk} high-risk*"
    stale_summary = ""
    if stale_count:
        stale_summary = f", {stale_count} stale"

    header = (
        f":bar_chart: *Open PR Dashboard* — {repo or 'repo'}"
        + f"\n*{total_open} open PRs{risk_summary}{stale_summary}*"
        + (f"\nAs of {as_of}" if as_of else "")
        + artifact_link
        + "\n\n"
    )

    # Prepend consistent metadata header to HTML artifact
    meta: list[str] = [f"**{total_open} open PRs**"]
    if high_risk:
        meta.append(f"{high_risk} high-risk")
    if stale_count:
        meta.append(f"{stale_count} stale")
    if as_of:
        meta.append(f"as of {as_of}")
    summary_line = f"**{high_risk} high-risk · {stale_count} stale**"
    md_header = build_md_report_header("Open PR Dashboard", repo or "repo", "", meta, summary_line)
    full_report_text = md_header + report_text

    title = f"Open PR Dashboard — {repo or 'repo'}"
    slack_text = format_for_slack(header + report_text)
    thread_ts = config.get("SLACK_THREAD_TS") or None
    slack_ok = post_report(
        config, slack_text, full_report_text,
        title=title, filename="open_prs_report.html",
        thread_ts=thread_ts,
    )

    result = {
        "status": "OK" if slack_ok else "SLACK_FAILED",
        "total_open": total_open,
        "high_risk": high_risk,
        "stale_count": stale_count,
        "report_bytes": len(full_report_text),
        "slack_posted": slack_ok,
    }
    (reports_dir / "post_open_prs_result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8",
    )
    print(
        f"[post-open-prs] total={total_open} high_risk={high_risk} stale={stale_count} "
        f"slack={'ok' if slack_ok else 'FAILED'}",
        flush=True,
    )
    if not slack_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
