"""Post merge-queue pipeline results to Slack and write report artifacts.

Usage:
    python -m scripts.mq.report
    python -m scripts.mq.report --title "Scope Router"

Optional env:
    SLACK_BOT_TOKEN   — if set, posts report to Slack
    SLACK_CHANNEL     — channel name
    SLACK_THREAD_TS   — reply in existing thread
    PR_NUMBER         — included in report title

Reads (whichever exist):
    /workspace/scope.json
    /workspace/risk_score.json
    /workspace/test_impact.json
    /workspace/test_summary.json
    /workspace/gate_result.json
    /workspace/lane_groups.json
    /workspace/shard_result_*.json

Writes:
    /workspace/reports/report.md
    /workspace/reports/post_result.json

Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.slack_format import format_for_slack
from scripts.standup.slack_client import post_report


def _read_json(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _build_report(workspace: Path, pr_number: str, title: str) -> tuple[str, dict]:
    """Build markdown report from available MQ result files.

    Returns (markdown_text, context_vars_for_task_context).
    """
    ctx: dict[str, str] = {}
    sections: list[str] = []

    heading = f"# {title}"
    if pr_number:
        heading += f" — PR #{pr_number}"
    sections.append(heading)
    sections.append("")

    scope = _read_json(workspace / "scope.json")
    if scope:
        s = scope.get("scope", "unknown")
        blast = scope.get("blast_radius", "unknown")
        files = scope.get("changed_files", 0)
        lines = scope.get("lines_changed", 0)
        sections.append("## Scope")
        sections.append("")
        sections.append(f"| Field | Value |")
        sections.append(f"|-------|-------|")
        sections.append(f"| Scope | **{s}** |")
        sections.append(f"| Blast radius | **{blast}** |")
        sections.append(f"| Changed files | {files} |")
        sections.append(f"| Lines changed | {lines} |")
        sensitive = scope.get("sensitive_touched", [])
        if sensitive:
            sections.append(f"| Sensitive paths | {', '.join(f'`{p}`' for p in sensitive[:5])} |")
        sections.append("")
        ctx["SCOPE"] = s
        ctx["BLAST_RADIUS"] = blast

    risk = _read_json(workspace / "risk_score.json")
    if risk:
        tier = risk.get("tier", "UNKNOWN")
        score = risk.get("score", 0)
        needs_approval = risk.get("requires_human_approval", False)
        emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(tier, "⚪")
        sections.append("## Risk Score")
        sections.append("")
        sections.append(f"{emoji} **{tier}** (score {score}/100)")
        sections.append("")
        dims = risk.get("dimensions", {})
        if dims:
            sections.append("| Dimension | Score |")
            sections.append("|-----------|-------|")
            for k, v in dims.items():
                sections.append(f"| {k.replace('_', ' ').title()} | {v} |")
            sections.append("")
        if needs_approval:
            sections.append("> ⚠️ **Human approval required**")
            sections.append("")
        ctx["RISK_TIER"] = tier
        ctx["RISK_SCORE"] = str(score)

    impact = _read_json(workspace / "test_impact.json")
    if impact:
        total = impact.get("total_tests", 0)
        selected = impact.get("selected_tests", 0)
        reduction = impact.get("reduction_pct", 0)
        shards = impact.get("shards", [])
        unmapped = impact.get("unmapped_files", [])
        sections.append("## Test Impact Analysis")
        sections.append("")
        sections.append(f"**{selected}** / {total} tests selected "
                        f"(**{reduction:.0f}%** reduction) across **{len(shards)}** shards")
        sections.append("")
        if unmapped:
            sections.append(f"⚠️ {len(unmapped)} changed files have no test mapping:")
            for f in unmapped[:10]:
                sections.append(f"- `{f}`")
            if len(unmapped) > 10:
                sections.append(f"- ... and {len(unmapped) - 10} more")
            sections.append("")
        ctx["TEST_REDUCTION_PCT"] = f"{reduction:.0f}"
        ctx["SELECTED_TESTS"] = str(selected)
        ctx["TOTAL_TESTS"] = str(total)

    summary = _read_json(workspace / "test_summary.json")
    if not summary:
        shard_files = sorted(glob.glob(str(workspace / "shard_result_*.json")))
        if shard_files:
            results = []
            for f in shard_files:
                try:
                    results.append(json.loads(Path(f).read_text()))
                except (json.JSONDecodeError, OSError):
                    pass
            if results:
                passed = sum(r.get("passed", 0) for r in results)
                failed = sum(r.get("failed", 0) for r in results)
                skipped = sum(r.get("skipped", 0) for r in results)
                total = passed + failed + skipped
                duration = max((r.get("duration_s", 0) for r in results), default=0)
                summary = {
                    "shards": len(results),
                    "total": total,
                    "passed": passed,
                    "failed": failed,
                    "skipped": skipped,
                    "wall_clock_s": round(duration, 1),
                    "status": "PASS" if failed == 0 else "FAIL",
                }

    if summary:
        passed = summary.get("passed", 0)
        failed = summary.get("failed", 0)
        total = summary.get("total", passed + failed)
        skipped = summary.get("skipped", 0)
        wall = summary.get("wall_clock_s", 0)
        status = summary.get("status", "UNKNOWN")
        emoji = "✅" if status == "PASS" else "❌"
        sections.append("## Test Results")
        sections.append("")
        sections.append(f"{emoji} **{passed}** / {total} passed")
        if failed:
            sections.append(f"  ❌ {failed} failed")
        if skipped:
            sections.append(f"  ⏭️ {skipped} skipped")
        sections.append(f"  ⏱️ {wall:.0f}s wall clock across "
                        f"{summary.get('shards', '?')} shards")
        sections.append("")
        ctx["TEST_STATUS"] = status
        ctx["TESTS_PASSED"] = str(passed)
        ctx["TESTS_FAILED"] = str(failed)

    gate = _read_json(workspace / "gate_result.json")
    if gate:
        needs = gate.get("needs_approval", False)
        reason = gate.get("reason", "")
        findings = gate.get("findings_count", 0)
        emoji = "🚦" if needs else "✅"
        sections.append("## Gate Decision")
        sections.append("")
        sections.append(f"{emoji} Approval required: **{needs}**")
        if reason:
            sections.append(f"  Reason: {reason}")
        if findings:
            sections.append(f"  Findings: {findings}")
        sections.append("")
        ctx["GATE_APPROVAL"] = str(needs).lower()

    lanes = _read_json(workspace / "lane_groups.json")
    if lanes:
        lane_list = lanes.get("lanes", [])
        total_prs = sum(len(l.get("prs", [])) for l in lane_list)
        sections.append("## Merge Queue Lanes")
        sections.append("")
        sections.append(f"**{len(lane_list)}** scope lanes, **{total_prs}** PRs queued")
        sections.append("")
        if lane_list:
            sections.append("| Lane | PRs |")
            sections.append("|------|-----|")
            for lane in lane_list:
                lid = lane.get("lane_id", "?")
                prs = lane.get("prs", [])
                pr_nums = ", ".join(f"#{p.get('pr_number', '?')}" for p in prs[:5])
                sections.append(f"| {lid} | {pr_nums} |")
            sections.append("")
        ctx["LANE_COUNT"] = str(len(lane_list))
        ctx["QUEUED_PRS"] = str(total_prs)

    return "\n".join(sections), ctx


def main() -> None:
    config = load_config(required=[])
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    pr_number = config.get("PR_NUMBER", "")
    title = config.get("REPORT_TITLE", "Merge Queue Report")

    report_text, ctx = _build_report(workspace, pr_number, title)

    print("\n" + "=" * 60, flush=True)
    print(report_text, flush=True)
    print("=" * 60 + "\n", flush=True)

    (reports_dir / "report.md").write_text(report_text)
    print("[mq-report] reports/report.md written", flush=True)

    for key, val in ctx.items():
        print(f"::add-task-context {key}::{val}", flush=True)

    from scripts.common.report_renderer import render_simple_html
    try:
        html = render_simple_html(title, report_text)
        (reports_dir / "report.html").write_text(html)
        print("[mq-report] reports/report.html written", flush=True)
    except Exception as e:
        print(f"[mq-report] HTML render failed (non-fatal): {e}", flush=True)

    slack_text = format_for_slack(report_text)

    from scripts.common.slack_format import build_artifact_links
    html_url, job_url = build_artifact_links(config, "report", "report.html")
    if html_url:
        slack_text += f"\n\n📎 <{html_url}|View full report>  |  <{job_url}|Job details>"

    thread_ts = config.get("SLACK_THREAD_TS") or config.get("SlackThreadTs") or None
    slack_ok = post_report(config, slack_text, report_text,
                           title=title, filename="mq_report.html",
                           thread_ts=thread_ts, task_type="report")

    result = {
        "status": "DONE",
        "slack_posted": slack_ok,
        **ctx,
    }
    (reports_dir / "result.json").write_text(json.dumps(result, indent=2))
    (reports_dir / "post_result.json").write_text(json.dumps(result, indent=2))

    print(
        f"[mq-report] done — slack_posted={slack_ok} "
        f"context_keys={','.join(ctx.keys()) or 'none'}",
        flush=True,
    )
    sys.exit(0)


if __name__ == "__main__":
    from scripts.common.entrypoint import run_main
    run_main(main, "reports/post_result.json")
