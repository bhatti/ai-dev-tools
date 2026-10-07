# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post Resync PR report to Slack.

Reads reports/resync_prs_summary.json and reports/resync_prs_report.md.
Posts a per-PR digest to Slack with a link to the full HTML artifact.

Usage:
    python -m scripts.resync.post_resync_prs

Env:
    WORKSPACE_DIR        workspace directory (default: /workspace)
    SLACK_BOT_TOKEN      Slack bot token
    SLACK_CHANNEL        channel to post to
    SLACK_THREAD_TS      optional thread timestamp
    FORMICARY_PUBLIC_URL base URL for artifact links
    JOB_ID               Formicary job request ID
    TASK_TYPE            must match the task_type that produced the artifacts
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


_STATUS_EMOJI = {
    "synced": "✅",
    "up_to_date": "✅",
    "conflict": "⚠️",
    "skipped": "⏭️",
    "error": "❌",
}


def _format_pr_lines(results: list[dict]) -> list[str]:
    lines: list[str] = []
    for r in results:
        emoji = _STATUS_EMOJI.get(r.get("status", ""), "❓")
        pr_num = r.get("pr_number", "?")
        pr_url = r.get("pr_url", "")
        pr_title = (r.get("pr_title") or "")[:50]
        pr_branch = r.get("pr_branch", "")
        base_branch = r.get("base_branch", "")
        status = r.get("status", "")
        commits_merged = r.get("commits_merged", 0)
        conflict_files = r.get("conflict_files") or []
        before_lines = r.get("before_diff_lines", 0)
        after_lines = r.get("after_diff_lines", 0)
        diff_verified = r.get("diff_verified", False)
        build_status = r.get("build_status", "unknown")
        rev = r.get("reviewers") or {}

        pr_ref = f"<{pr_url}|#{pr_num}>" if pr_url else f"#{pr_num}"
        lines.append(
            f"*{pr_ref}* — {pr_title}  `{pr_branch} → {base_branch}`"
        )

        if status == "synced":
            diff_str = (
                f"diff: {before_lines}→{after_lines} ✅" if diff_verified else "diff: ❌"
            )
            ci_str = {"pass": "CI: ✅", "fail": "CI: ❌", "pending": "CI: ⏳"}.get(
                build_status, ""
            )
            parts = [f"✅ *Synced* (+{commits_merged} commit(s))", diff_str]
            if ci_str:
                parts.append(ci_str)
            if rev.get("approved"):
                parts.append(f"👥 {rev['approved']} approved")
            lines.append("  " + "  |  ".join(parts))

        elif status == "up_to_date":
            ci_str = {"pass": "CI: ✅", "fail": "CI: ❌"}.get(build_status, "")
            parts = ["✅ *Up to date*"]
            if ci_str:
                parts.append(ci_str)
            if rev.get("approved"):
                parts.append(f"👥 {rev['approved']} approved")
            lines.append("  " + "  |  ".join(parts))

        elif status == "conflict":
            cf = ", ".join(f"`{f}`" for f in conflict_files[:3])
            suffix = f" (+{len(conflict_files) - 3} more)" if len(conflict_files) > 3 else ""
            lines.append(
                f"  ⚠️ *Conflicts* — {cf}{suffix}  |  manual resolution needed"
            )

        elif status == "skipped":
            lines.append("  ⏭️ *Skipped* — not authored by current user")

        elif status == "error":
            err = (r.get("error") or "unknown error")[:100]
            lines.append(f"  ❌ *Error* — {err}")

        lines.append("")
    return lines


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    reports_dir = workspace_dir / "reports"

    report_path = reports_dir / "resync_prs_report.md"
    if not report_path.exists():
        print(
            "ERROR: resync_prs_report.md not found — run resync-prs step first",
            file=sys.stderr,
        )
        sys.exit(1)

    report_text = report_path.read_text(encoding="utf-8")

    # Load summary JSON
    total = synced = up_to_date = conflicts = errors = skipped = 0
    repo = tracker = as_of = ""
    dry_run = False
    results: list[dict] = []

    summary_path = reports_dir / "resync_prs_summary.json"
    if summary_path.exists():
        try:
            s = json.loads(summary_path.read_text(encoding="utf-8"))
            total = s.get("total", 0)
            synced = s.get("synced", 0)
            up_to_date = s.get("up_to_date", 0)
            conflicts = s.get("conflicts", 0)
            errors = s.get("errors", 0)
            skipped = s.get("skipped", 0)
            repo = s.get("repo", "")
            tracker = s.get("tracker", "")
            as_of = s.get("as_of", "")
            dry_run = s.get("dry_run", False)
            results = s.get("results", [])
        except Exception as exc:
            print(
                f"[post-resync-prs] warn: could not parse summary JSON: {exc}",
                flush=True,
            )

    reports_url, _ = build_artifact_links(config)
    artifact_link = f"\n📎 <{reports_url}|View Full Report>" if reports_url else ""

    # Status summary line
    status_parts = []
    if synced:
        status_parts.append(f"{synced} synced")
    if up_to_date:
        status_parts.append(f"{up_to_date} up-to-date")
    if conflicts:
        status_parts.append(f"*{conflicts} conflict(s)*")
    if errors:
        status_parts.append(f"{errors} error(s)")
    if skipped:
        status_parts.append(f"{skipped} skipped")

    dry_label = "  _(dry-run — no changes pushed)_" if dry_run else ""
    status_line = (
        f"*{', '.join(status_parts)}*" if status_parts else f"*{total} PRs*"
    )
    header = (
        f":arrows_counterclockwise: *PR Resync Report* — {repo or 'repo'}{dry_label}"
        + f"\n{total} PRs: {status_line}"
        + (f"\n{as_of}" if as_of else "")
        + artifact_link
        + "\n\n"
    )

    pr_lines = _format_pr_lines(results)

    # HTML/MD report with consistent header
    meta = [f"**{total} PRs**"]
    if synced:
        meta.append(f"{synced} synced")
    if conflicts:
        meta.append(f"{conflicts} conflict(s)")
    if dry_run:
        meta.append("dry-run")
    summary_line_md = (
        f"**{synced} synced · {up_to_date} up-to-date · {conflicts} conflict(s)**"
    )
    md_header = build_md_report_header(
        "PR Resync Report", repo or "repo", "", meta, summary_line_md
    )
    full_report_text = md_header + report_text

    slack_body = header + "\n".join(pr_lines)
    title = f"PR Resync Report — {repo or 'repo'}"
    slack_text = format_for_slack(slack_body)
    thread_ts = config.get("SLACK_THREAD_TS") or None

    slack_ok = post_report(
        config, slack_text, full_report_text,
        title=title, filename="resync_prs_report.html",
        thread_ts=thread_ts,
    )

    result_data = {
        "status": "OK" if slack_ok else "SLACK_FAILED",
        "total": total,
        "synced": synced,
        "conflicts": conflicts,
        "report_bytes": len(full_report_text),
        "slack_posted": slack_ok,
    }
    (reports_dir / "post_resync_prs_result.json").write_text(
        json.dumps(result_data, indent=2), encoding="utf-8"
    )
    print(
        f"[post-resync-prs] total={total} synced={synced} conflicts={conflicts} "
        f"slack={'ok' if slack_ok else 'FAILED (report in artifacts)'}",
        flush=True,
    )
    # Exit 0 even when Slack fails — PR syncs already completed successfully and
    # the HTML/MD report is available in artifacts. Failing here would route to
    # notify-error which would also fail with the same broken token, masking the
    # actual sync results in the job status.
    if not slack_ok:
        print(
            "[post-resync-prs] Slack post failed but sync results are in artifacts "
            f"({len(full_report_text)} bytes) — exiting 0 to preserve job status",
            flush=True,
        )


if __name__ == "__main__":
    main()
