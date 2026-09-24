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
    /workspace/test_summary.json      (when absent and shard files or API data are available)
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

import requests
import urllib3

# Formicary public URLs typically use self-signed certs in dev/staging.
# This module is always a standalone entrypoint — no other HTTP clients share this process,
# so the module-level suppression does not affect unrelated code.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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


def _fetch_shard_results_from_api(
        item_var: str = "shard",
        fan_out_task_type: str = "run-tests",
) -> list[dict]:
    """Fetch per-shard results from the Formicary API using task context variables.

    FanOutTasklet prefixes each fan-out child's task context with "{item_var}_{idx}_".
    run_scoped_ci emits ::add-task-context ShardResult::{json}, so the parent
    run-tests task execution has keys like shard_0_ShardResult, shard_1_ShardResult.

    Returns a list of shard result dicts, sorted by shard_id. Returns [] on any error
    or when FORMICARY_TOKEN / FORMICARY_PUBLIC_URL / JOB_ID env vars are absent.
    """
    base_url = os.environ.get("FORMICARY_PUBLIC_URL", "").rstrip("/")
    token = os.environ.get("FORMICARY_TOKEN", "")
    job_id = os.environ.get("JOB_ID", "")
    if not (base_url and token and job_id):
        return []

    headers = {"Authorization": f"Bearer {token}"}
    try:
        # Get job request to find execution id
        resp = requests.get(f"{base_url}/api/jobs/requests/{job_id}",
                            headers=headers, timeout=15, verify=False)
        if not resp.ok:
            return []
        exec_id = (resp.json().get("job_request") or resp.json()).get("job_execution_id", "")
        if not exec_id:
            return []

        # Get job execution with task contexts
        resp = requests.get(f"{base_url}/api/v1/jobs/executions/{exec_id}",
                            headers=headers, timeout=15, verify=False)
        if not resp.ok:
            return []
        je = resp.json().get("job_execution") or resp.json()

        # Find the fan-out task execution and extract shard context keys
        for task in je.get("tasks") or []:
            if task.get("task_type") != fan_out_task_type:
                continue
            prefix = f"{item_var}_"
            suffix = "_ShardResult"
            shard_results = []
            for ctx in task.get("contexts") or []:
                name = ctx.get("name", "")
                # Key format: shard_0_ShardResult, shard_1_ShardResult ...
                if name.startswith(prefix) and name.endswith(suffix):
                    try:
                        shard_results.append(json.loads(ctx.get("value", "{}")))
                    except json.JSONDecodeError:
                        pass
            if shard_results:
                return sorted(shard_results, key=lambda r: r.get("shard_id", 0))
    except Exception:
        pass
    return []


_SCOPE_DESCRIPTIONS = {
    "cross-scope": "Files span 2+ teams or top-level directories — must serialize in merge queue",
    "empty": "No changed files detected (metadata-only change)",
}

_BLAST_DESCRIPTIONS = {
    "low": "Small, contained change (≤50 lines, 1 module) — safe to batch with other PRs",
    "medium": "Moderate change (51–300 lines or 2 modules) — test independently before merge",
    "high": "Large or security-sensitive change (>300 lines, 3+ modules, or sensitive paths) — requires human approval",
}

_RISK_DIM_DESCRIPTIONS = {
    "size": "Total lines changed (additions + deletions). Larger PRs have more surface area for defects",
    "file_count": "Number of files modified. More files = more integration points that could break",
    "blast_radius": "How many modules/teams affected. Cross-scope changes multiply interaction failure risk",
    "sensitive_paths": "Files touching auth, security, billing, or infrastructure. Defects here have outsized production impact",
    "test_coverage": "Ratio of test files to source files. No tests = changes ship unverified (lower is better)",
    "historical": "Recent defect rate for changed paths. Areas with frequent past defects produce more",
}



def _dim_evidence(dim: str, score: int, additions: int, deletions: int,
                   n_files: int, scope_data: dict | None, risk_scope: str,
                   has_historical_data: bool = False) -> str:
    """Return a short evidence string explaining why a dimension got its score."""
    total_loc = additions + deletions
    if dim == "size":
        return f"{total_loc} lines (+{additions}/−{deletions})"
    if dim == "file_count":
        return f"{n_files} files changed"
    if dim == "blast_radius":
        blast = (scope_data or {}).get("blast_radius", "unknown")
        return f"blast={blast}, scope={risk_scope}"
    if dim == "sensitive_paths":
        sensitive = (scope_data or {}).get("touches", []) or (scope_data or {}).get("sensitive_touched", [])
        if sensitive:
            shown = ", ".join(sensitive[:3])
            extra = f" +{len(sensitive)-3} more" if len(sensitive) > 3 else ""
            return f"{len(sensitive)} sensitive files: {shown}{extra}"
        return "no sensitive files detected"
    if dim == "test_coverage":
        if score == 0:
            return "test:source ratio ≥1:1"
        if score <= 2:
            return "test:source ratio ≥1:2"
        if score <= 4:
            return "test:source ratio ≥1:4"
        if score <= 6:
            return "few test files relative to source"
        return "no test files in PR"
    if dim == "historical":
        if has_historical_data:
            return f"from defect_history.json (score={score})"
        return "⚠️ no defect history available — using neutral default"
    return ""


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
        owners = scope.get("owners", [])
        scope_desc = _SCOPE_DESCRIPTIONS.get(s, f"All changes owned by {s} — can merge in dedicated lane")
        blast_desc = _BLAST_DESCRIPTIONS.get(blast, "Unknown blast radius")
        sections.append("## Scope")
        sections.append("")
        sections.append("| Field | Value | Description |")
        sections.append("|-------|-------|-------------|")
        sections.append(f"| Scope | **{s}** | {scope_desc} |")
        sections.append(f"| Blast radius | **{blast}** | {blast_desc} |")
        sections.append(f"| Changed files | {files} | Number of files modified in this PR |")
        sections.append(f"| Lines changed | {lines} | Total additions + deletions |")
        if owners:
            sections.append(f"| Owners | {', '.join(owners)} | CODEOWNERS entries responsible for review |")
        sensitive = scope.get("touches", []) or scope.get("sensitive_touched", [])
        if sensitive:
            sections.append(f"| Sensitive paths | {', '.join(f'`{p}`' for p in sensitive[:5])} | Files matching auth/security/billing/infra patterns |")
        sections.append("")
        ctx["SCOPE"] = s
        ctx["BLAST_RADIUS"] = blast

    risk = _read_json(workspace / "risk_score.json")
    if risk:
        tier = risk.get("tier", "UNKNOWN")
        score = risk.get("score", 0)
        needs_approval = risk.get("requires_human_approval", False)
        tier_descs = {
            "LOW": "Safe to auto-merge after CI passes; can batch with other low-risk PRs",
            "MEDIUM": "Standard review needed; test independently before merge",
            "HIGH": "Requires human approval; test in isolation before merge",
        }
        emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(tier, "⚪")
        sections.append("## Risk Score")
        sections.append("")
        sections.append(f"{emoji} **{tier}** (score {score}/100) — {tier_descs.get(tier, 'Unknown tier')}")
        sections.append("")
        dims = risk.get("dimensions", {})
        weights = risk.get("weights", {})
        additions = risk.get("additions", 0)
        deletions = risk.get("deletions", 0)
        n_files = risk.get("changed_files", 0)
        risk_scope = risk.get("scope", "unknown")
        has_hist = risk.get("has_historical_data", False)
        if dims:
            sections.append("| Dimension | Score | Weight | Evidence | Description |")
            sections.append("|-----------|-------|--------|----------|-------------|")
            for k, v in dims.items():
                label = k.replace("_", " ").title()
                w_raw = weights.get(k)
                w = f"{w_raw}×" if isinstance(w_raw, (int, float)) else str(w_raw or "")
                desc = _RISK_DIM_DESCRIPTIONS.get(k, "")
                evidence = _dim_evidence(k, v, additions, deletions, n_files,
                                         scope, risk_scope, has_hist)
                sections.append(f"| {label} | {v}/10 | {w} | {evidence} | {desc} |")
            sections.append("")
        if dims and weights:
            breakdown_parts = []
            computed_total = 0.0
            for k, v in dims.items():
                w = weights.get(k, 1.0)
                contribution = v * w
                computed_total += contribution
                breakdown_parts.append(f"{k.replace('_',' ')}={v}×{w}={contribution:.1f}")
            sections.append(f"> **Score breakdown:** {' + '.join(breakdown_parts)} = **{computed_total:.1f}**")
            sections.append("")
        if not has_hist and "historical" in dims:
            sections.append("> ℹ️ Historical dimension uses neutral default (3/10) — "
                            "provide `defect_history.json` for data-driven scoring")
            sections.append("")
        if needs_approval:
            sections.append("> ⚠️ **Human approval required** — risk score exceeds auto-merge threshold (≥31)")
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
    # Load shard files once; reused for both building summary (when absent) and the perf table.
    shard_results: list[dict] = []
    for f in sorted(glob.glob(str(workspace / "shard_result_*.json"))):
        try:
            shard_results.append(json.loads(Path(f).read_text()))
        except (json.JSONDecodeError, OSError):
            pass

    if not summary:
        # Primary: artifact files; fallback: API task context if artifacts are absent.
        if not shard_results:
            # Compat: old images wrote the un-indexed shard_result.json; the glob above
            # (shard_result_*.json) won't match it. Handle gracefully during rolling deploy.
            compat = workspace / "shard_result.json"
            if compat.exists():
                try:
                    shard_results = [json.loads(compat.read_text())]
                except (json.JSONDecodeError, OSError):
                    pass
        if not shard_results:
            shard_results = _fetch_shard_results_from_api()
        if shard_results:
            passed = sum(r.get("passed", 0) for r in shard_results)
            failed = sum(r.get("failed", 0) for r in shard_results)
            skipped = sum(r.get("skipped", 0) for r in shard_results)
            total = passed + failed + skipped
            duration = max((r.get("duration_s", 0) for r in shard_results), default=0)
            summary = {
                "shards": len(shard_results),
                "total": total,
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "wall_clock_s": round(duration, 1),
                "status": "PASS" if failed == 0 else "FAIL",
            }
            # Write test_summary.json as an artifact for downstream consumers and dashboards.
            (workspace / "test_summary.json").write_text(json.dumps(summary, indent=2))

    if summary:
        passed = summary.get("passed", 0)
        failed = summary.get("failed", 0)
        total = summary.get("total", passed + failed)
        skipped = summary.get("skipped", 0)
        wall = summary.get("wall_clock_s", 0)
        status = summary.get("status", "UNKNOWN")
        n_shards = summary.get("shards", "?")
        emoji = "✅" if status == "PASS" else "❌"
        sections.append("## Test Results")
        sections.append("")
        sections.append(f"{emoji} **{passed}** / {total} passed")
        if failed:
            sections.append(f"  ❌ {failed} failed")
        if skipped:
            sections.append(f"  ⏭️ {skipped} skipped")
        sections.append(f"  ⏱️ {wall:.0f}s wall clock across {n_shards} shards")
        sections.append("")

        if len(shard_results) > 1:
            durations = [r.get("duration_s", 0) for r in shard_results]
            total_sequential = sum(durations)
            wall_parallel = max(durations) if durations else 0
            speedup = total_sequential / wall_parallel if wall_parallel > 0 else 1.0
            sections.append("### Shard Performance")
            sections.append("")
            sections.append("| Shard | Tests | Passed | Failed | Duration | Status |")
            sections.append("|-------|-------|--------|--------|----------|--------|")
            for r in sorted(shard_results, key=lambda x: x.get("duration_s", 0), reverse=True):
                sid = r.get("shard_id", "?")
                s_passed = r.get("passed", 0)
                s_failed = r.get("failed", 0)
                s_total = s_passed + s_failed + r.get("skipped", 0)
                s_dur = r.get("duration_s", 0)
                s_status = "✅" if r.get("status") == "passed" else "❌"
                sections.append(f"| {sid} | {s_total} | {s_passed} | {s_failed} | {s_dur:.1f}s | {s_status} |")
            sections.append("")
            sections.append(f"> **Parallel speedup:** {total_sequential:.0f}s sequential → "
                            f"{wall_parallel:.0f}s parallel ({speedup:.1f}x across {len(shard_results)} shards)")
            sections.append("")

            all_slow: list[dict] = []
            for r in shard_results:
                for st in r.get("slow_tests", []):
                    all_slow.append(st)
            all_slow.sort(key=lambda x: x.get("duration_s", 0), reverse=True)
            if all_slow:
                sections.append("### Slowest Tests")
                sections.append("")
                sections.append("| Duration | Test |")
                sections.append("|----------|------|")
                for st in all_slow[:10]:
                    sections.append(f"| {st['duration_s']:.2f}s | `{st['name']}` |")
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

    thread_ts = config.get("SLACK_THREAD_TS") or config.get("SlackThreadTs") or None
    slack_ok = post_report(config, slack_text, report_text,
                           title=title, filename="report.html",
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
