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
from collections import Counter
from pathlib import Path

import requests
import urllib3

# Formicary public URLs typically use self-signed certs in dev/staging.
# This module is always a standalone entrypoint — no other HTTP clients share this process,
# so the module-level suppression does not affect unrelated code.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.report_utils import write_report
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
    except Exception as exc:
        print(f"[mq-report] shard API fetch failed: {exc}", flush=True)
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
            return "good coverage (test:source ≥1:1)"
        if score <= 2:
            return "moderate coverage (test:source 0.5–1.0)"
        if score <= 4:
            return "low coverage (test:source 0.25–0.5)"
        if score <= 6:
            return "sparse coverage (test:source <0.25)"
        return "no test files in changed files"
    if dim == "historical":
        if has_historical_data:
            return f"from defect_history.json (score={score})"
        return "⚠️ no defect history available — using neutral default"
    return ""


_SLEEP_KEYWORDS = ("sleep", "async", "wait", "delay", "timeout", "poll", "retry", "init")
_SLEEP_THRESHOLD_S = 5.0


def _emit_test_health_insights(
    sections: list[str],
    shard_results: list[dict],
    all_slow: list[dict],
    passed: int,
    failed: int,
) -> None:
    """Append a Test Health Insights section with actionable analysis."""
    insights: list[str] = []

    # Pass rate
    total = passed + failed
    if total > 0:
        pass_rate = passed / total * 100
        if pass_rate < 100:
            insights.append(f"⚠️ Pass rate: {pass_rate:.1f}% ({failed} failure{'s' if failed != 1 else ''})")
        else:
            insights.append(f"✅ Pass rate: 100% ({passed} tests)")

    # Shard balance — how evenly tests were distributed
    if len(shard_results) > 1:
        durations = [r.get("duration_s", 0) for r in shard_results]
        max_dur = max(durations)
        min_dur = min(durations)
        if max_dur > 0:
            imbalance_pct = (max_dur - min_dur) / max_dur * 100
            if imbalance_pct > 30:
                insights.append(
                    f"⚠️ Shard imbalance: {imbalance_pct:.0f}% — "
                    f"slowest shard ({max_dur:.0f}s) vs fastest ({min_dur:.0f}s); "
                    "redistribute tests by duration for better parallelism"
                )
            else:
                insights.append(
                    f"✅ Shard balance: {imbalance_pct:.0f}% imbalance "
                    f"({min_dur:.0f}s – {max_dur:.0f}s)"
                )

    # Detect sleep-based / timing-sensitive tests from slow list
    sleep_suspects: list[str] = []
    seen_slow_names: dict[str, int] = {}
    for st in all_slow:
        name = st.get("name", "")
        dur = st.get("duration_s", 0)
        name_lower = name.lower()
        if dur >= _SLEEP_THRESHOLD_S and any(kw in name_lower for kw in _SLEEP_KEYWORDS):
            sleep_suspects.append(f"`{name}` ({dur:.1f}s)")
        # Track duplicates across shards (same test name in multiple shards)
        seen_slow_names[name] = seen_slow_names.get(name, 0) + 1

    if sleep_suspects:
        insights.append(
            f"🕐 {len(sleep_suspects)} slow test{'s' if len(sleep_suspects) != 1 else ''} "
            f"likely using real sleeps — consider mocking time or reducing timeouts: "
            + ", ".join(sleep_suspects[:5])
        )

    # Detect tests appearing in multiple shards (name collision = same test ran in 2+ shards)
    dup_names = [n for n, c in seen_slow_names.items() if c > 1]
    if dup_names:
        insights.append(
            f"⚠️ {len(dup_names)} slow test name{'s' if len(dup_names) != 1 else ''} "
            f"appear in multiple shards (possible test duplication): "
            + ", ".join(f"`{n}`" for n in dup_names[:5])
        )

    if not insights:
        return

    sections.append("### Test Health Insights")
    sections.append("")
    for item in insights:
        sections.append(f"- {item}")
    sections.append("")


from scripts.common.pr_classify import (
    HIGH_BLAST_CATEGORIES as _HIGH_BLAST_CATEGORIES,
    PR_TYPE_EMOJI as _PR_TYPE_EMOJI,
    RISK_EMOJI as _RISK_EMOJI,
    _compute_pr_age_inline,
    build_category_breakdown as _build_category_breakdown_shared,
    build_work_type_distribution as _build_work_type_distribution_shared,
    build_metrics_dashboard as _build_metrics_dashboard,
    build_stale_pr_table as _build_stale_pr_table,
    compute_throughput_metrics as _compute_throughput_metrics,
    format_pr_status as _format_pr_status,
)

_RISK_TIER_ORDER: list[str] = ["high", "medium", "low"]


def _pr_link(pr: dict) -> str:
    """Format a PR number as a markdown link."""
    num = pr.get("pr_number", "?")
    url = pr.get("url", "")
    return f"[#{num}]({url})" if url else f"#{num}"


def _valley_of_calm_section(prs: list[dict], lanes: list[dict] | None = None) -> str:
    """Build queue health section using simulation-inspired metrics.

    Derives proxy parameters from actual PR data:
    - defect_prob proxy: fraction of PRs with failed CI
    - batch_size proxy: average PRs per canonical lane (~total / estimated lanes)

    Formula: batch_success = (1 - defect_prob) ^ batch_size
    All numbers are from actual PR data — no fabricated estimates.
    """
    if not prs:
        return ""
    from collections import Counter
    from scripts.mq.simulate import (
        build_risk_heatmap,
        deployment_risk_summary,
        load_deployment_profile,
        merge_batch_success,
    )
    total = len(prs)
    failed_ci = sum(1 for p in prs if p.get("ci_status") == "fail")
    aged = sum(1 for p in prs if p.get("age_hours", 0) > 48)
    high_blast = sum(1 for p in prs if p.get("risk_tier", p.get("blast_radius")) == "high")
    medium_blast = sum(1 for p in prs if p.get("risk_tier", p.get("blast_radius")) == "medium")
    # CI data availability: BB bulk API never sets ci_status from actual pipeline runs.
    # When ALL PRs show "unknown", treat CI as unavailable and use aged PRs as health proxy.
    ci_available = any(p.get("ci_status") not in ("unknown", None, "") for p in prs)
    defect_prob = (failed_ci / total if ci_available and total > 0 else 0.0)
    estimated_lanes = max(1, total // 10)
    avg_batch = round(total / estimated_lanes, 1)
    aged_pct = round(aged / total * 100, 1)
    ci_pct = round(defect_prob * 100, 1)

    # Health assessment: when CI unavailable, use aged PRs as primary pressure signal
    if ci_available:
        batch_success = round(merge_batch_success(defect_prob, avg_batch) * 100, 1) if avg_batch > 0 else 100.0
        health = "🟢 Healthy" if batch_success >= 90 else ("🟡 At Risk" if batch_success >= 70 else "🔴 Unstable")
        health_basis = f"CI failure rate {ci_pct}% at batch size ~{avg_batch}"
    else:
        # Fall back to aged PRs as pressure indicator
        batch_success = max(0.0, round(100.0 - aged_pct * 0.5, 1))  # heuristic: each 2% aged = 1% health loss
        health = "🟢 Healthy" if aged_pct < 20 else ("🟡 At Risk" if aged_pct < 50 else "🔴 Unstable")
        health_basis = f"{aged_pct}% PRs aged >48h (CI status unavailable from API)"

    if aged_pct < 20:
        advice = "Queue pressure is low — safe to batch."
    elif aged_pct < 50:
        advice = (
            f"At Risk: {aged_pct}% of PRs are aged >48h. "
            "Review blockers, unblock stale PRs, or reduce batch size to lower queue pressure."
        )
    else:
        advice = (
            f"Unstable: {aged_pct}% of PRs are aged >48h. "
            "Queue is backing up — prioritize unblocking stale PRs, "
            "increase review throughput, or split large PRs to reduce cycle time."
        )

    ci_row = (
        f"| CI failure rate | {failed_ci}/{total} ({ci_pct}%) |"
        if ci_available
        else "| CI failure rate | N/A — not reported by API (verify in CI dashboard) |"
    )

    lines = [
        "## Queue Health",
        "",
        f"Overall status: **{health}** (basis: {health_basis})",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total PRs in queue | {total} |",
        ci_row,
        f"| PRs aged >48h | {aged} ({aged_pct}%) |",
        f"| High blast-radius PRs | {high_blast} |",
        f"| Medium blast-radius PRs | {medium_blast} |",
        f"| Approx batch size (PRs/lane) | {avg_batch} |",
        "",
        f"> {advice}",
        "",
    ]

    lines += _build_category_breakdown_shared(prs, lanes=lanes)
    lines += _build_work_type_distribution_shared(prs)

    type_counts = Counter(p.get("pr_type", "unknown") for p in prs)
    n_bugs = type_counts.get("bug", 0) + type_counts.get("security", 0)

    if total > 0:
        # Defect rate proxy — maps to Joe's model
        defect_pct = round(n_bugs / total * 100, 1)
        if n_bugs > 0:
            defect_ratio = round(total / n_bugs)
            batch_success_est = round(merge_batch_success(n_bugs / total, avg_batch) * 100, 1)
            if defect_pct > 15:
                health_emoji = "🔴"
                health_label = "high defect rate"
            elif defect_pct > 8:
                health_emoji = "🟡"
                health_label = "moderate defect rate"
            else:
                health_emoji = "🟢"
                health_label = "healthy defect rate"
            lines.append(
                f"> {health_emoji} **Defect rate proxy: {defect_pct}%** "
                f"({n_bugs} bug+security PRs / {total} total). "
                f"At batch size ~{avg_batch}, est. batch success ≈ {batch_success_est}% "
                f"({health_label})."
            )
            lines.append("")
            feature_count = type_counts.get("feature", 0)
            if feature_count > 0:
                fb_ratio = round(feature_count / n_bugs, 1) if n_bugs else float("inf")
                lines.append(
                    f"> Feature:Bug ratio = **{fb_ratio}:1** "
                    f"({feature_count} features / {n_bugs} bugs). "
                    + ("Healthy — shipping more value than fixing." if fb_ratio >= 3
                       else "Below 3:1 — team is spending significant effort on defect repair."
                       if fb_ratio >= 1
                       else "⚠️ More bugs than features — investigate root causes.")
                )
                lines.append("")
        else:
            lines.append(f"> 🟢 No bug or security PRs in queue — defect rate proxy: 0%")
            lines.append("")

    # Review health — unreviewed high-risk PRs, review depth
    high_risk_prs = [p for p in prs if p.get("risk_tier", p.get("blast_radius")) == "high"]
    unreviewed_high = [p for p in high_risk_prs if p.get("reviewer_count", 0) == 0]
    no_reviewers = sum(1 for p in prs if p.get("reviewer_count", 0) == 0)
    approved = sum(1 for p in prs if p.get("approval_count", 0) > 0)

    if total > 0:
        lines.append("### Review Health")
        lines.append("")
        lines.append("| Metric | Value | Signal |")
        lines.append("|--------|-------|--------|")
        lines.append(f"| High-risk PRs | {len(high_risk_prs)} ({round(len(high_risk_prs)/total*100)}%) "
                     f"| {'🔴 >30%' if len(high_risk_prs)/total > 0.3 else '🟢 healthy'} |")
        lines.append(f"| Unreviewed (no assignees) | {no_reviewers}/{total} "
                     f"| {'⚠️ review gap' if no_reviewers > total * 0.5 else '—'} |")
        if unreviewed_high:
            lines.append(f"| Unreviewed high-risk | {len(unreviewed_high)}/{len(high_risk_prs)} "
                         f"| 🔴 high-risk PRs without reviewers |")
        if approved > 0:
            lines.append(f"| Approved | {approved}/{total} ({round(approved/total*100)}%) "
                         f"| ready to merge |")
        lines.append("")

    # Metrics Dashboard — consistent with pr-audit format
    defect_pct_for_dash = round(n_bugs / total * 100, 1) if total else 0.0
    cfr_signal = "🟢" if defect_pct_for_dash <= 10 else ("🟡" if defect_pct_for_dash <= 25 else "🔴")

    feature_count_for_dash = type_counts.get("feature", 0)
    fb_ratio = round(feature_count_for_dash / n_bugs, 1) if n_bugs else None
    fb_signal = "🟢" if (fb_ratio is not None and fb_ratio >= 3) else ("🟡" if fb_ratio is not None else "—")
    fb_value = f"{fb_ratio}:1" if fb_ratio is not None else "N/A"

    mq_extra_rows = [
        ("Queue Depth", str(total), "≤20", "🟢" if total <= 20 else "🔴", "Total PRs in the merge queue"),
        ("CFR Proxy", f"{defect_pct_for_dash}%", "≤10% healthy", cfr_signal,
         "Bug+security PRs as % of queue — proxy for change failure rate"),
        ("Feature:Bug Ratio", fb_value, "≥3:1 healthy", fb_signal,
         "Feature PRs / bug+security PRs — healthy teams ship more value than fixes"),
    ]
    if lanes:
        mq_extra_rows.append(("Lane Count", str(len(lanes)), "—", "—", "Active merge queue lanes"))
    # DORA rows: computed from merged PRs; if queue has only open PRs, show N/A so the
    # dashboard remains structurally consistent with pr-audit reports.
    if not _compute_throughput_metrics(prs):
        mq_extra_rows.extend([
            ("Deployment Frequency", "N/A", "≥5/wk", "—", "Requires merged PR history (queue shows open PRs only)"),
            ("Change Failure Rate", "N/A", "≤10%", "—", "Requires merged PR history (queue shows open PRs only)"),
            ("Lead Time (P50)", "N/A", "≤1d elite", "—", "Requires merged PR history (queue shows open PRs only)"),
            ("PR Survival Rate", "N/A", "≥80%", "—", "Requires merged PR history (queue shows open PRs only)"),
        ])
    lines.append(_build_metrics_dashboard(prs, extra_rows=mq_extra_rows))

    # Stale / obsolete PR flagging — use shared builder (>14 days)
    stale_lines = _build_stale_pr_table(prs, threshold_days=14)
    if stale_lines:
        stale_14d = [p for p in prs if _compute_pr_age_inline(p) > 14]
        stale_30d = [p for p in prs if _compute_pr_age_inline(p) > 30]
        stale_60d = [p for p in prs if _compute_pr_age_inline(p) > 60]
        # Prepend count header with 30d/60d breakdown
        if stale_60d:
            lines.append("> 🔴 PRs open >60 days are likely obsolete — consider closing or rebasing.")
        elif stale_30d:
            lines.append("> 🟡 PRs open >30 days accumulate merge conflicts and may need rebase.")
        if stale_30d or stale_60d:
            lines.append("")
        lines.extend(stale_lines)

    # Default to weekly-train when no profile explicitly configured
    profile_name = os.environ.get("DEPLOYMENT_PROFILE", "").strip() or "weekly-train"
    if total > 0:
        deploy_lines = _deployment_risk_lines(
            profile_name, total, type_counts, avg_batch,
            load_deployment_profile, deployment_risk_summary,
        )
        lines.extend(deploy_lines)

        n_defect = type_counts.get("bug", 0) + type_counts.get("security", 0)
        defect_rate = n_defect / total if total > 0 else 0.0
        lines.extend(build_risk_heatmap(defect_rate, avg_batch))

    return "\n".join(lines)


def _deployment_risk_lines(
    profile_name: str,
    total: int,
    type_counts: dict,
    avg_batch: float,
    load_profile_fn,
    risk_summary_fn,
) -> list[str]:
    """Render the Deployment Risk Position sub-section.

    Extracted from _valley_of_calm_section to keep CC manageable.
    Returns a list of markdown lines (empty if profile is invalid).
    """
    n_defect_prs = type_counts.get("bug", 0) + type_counts.get("security", 0)
    deploy_defect_rate = n_defect_prs / total if total > 0 else 0.0

    profile = load_profile_fn(profile_name, queue_size=total)
    if not profile:
        return []

    risk = risk_summary_fn(
        defect_rate=deploy_defect_rate,
        merge_batch_size=avg_batch,
        prs_per_release=profile["prs_per_release"],
        maturity_capabilities=profile["maturity"],
        releases_stacked=profile["releases_stacked"],
    )

    lines: list[str] = []
    lines.append("### Deployment Risk Position")
    lines.append("")

    gauge = risk["gauge"]
    lines.append(f"{gauge['bar']}  {gauge['description']}")
    lines.append("")

    cadence = profile.get("release_cadence", "unknown")
    prs_rel = profile["prs_per_release"]
    r_success = round(risk["release_train_success"] * 100, 1)
    adj_success = round(risk["adjusted_release_success"] * 100, 1)

    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Release cadence | {cadence} ({prs_rel} PRs/release) |")
    lines.append(f"| Release train success | {r_success}% (raw) → {adj_success}% (maturity-adjusted) |")

    rb = risk["rollback"]
    rb_emoji = "✅" if rb["can_rollback"] else "⚠️"
    lines.append(f"| Rollback feasibility | {rb_emoji} {rb['strategy']} "
                 f"(MTTR ×{rb['mttr_multiplier']}) |")
    lines.append(f"| Releases stacked | {profile['releases_stacked']} |")

    mat = risk["maturity"]
    tier_emoji = {"foundational": "🔴", "intermediate": "🟡", "advanced": "🟢"}.get(
        mat["maturity_tier"], "⚪")
    lines.append(f"| Deployment maturity | {tier_emoji} {mat['maturity_tier']} "
                 f"({mat['maturity_score']}/{mat['maturity_max']}) |")
    lines.append(f"| Risk multiplier | ×{mat['effective_risk_multiplier']} |")
    lines.append("")

    cal = risk["calamity"]
    headroom = cal["headroom_pct"]
    max_safe = cal.get("max_safe_batch")
    if cal["status"] == "red":
        lines.append(
            f"> 🔴 **Calamity zone** — current release success ({adj_success}%) is below "
            f"the 70% threshold. Reduce batch size or improve deployment maturity."
        )
    elif cal["status"] == "yellow":
        max_safe_str = f"{max_safe:.0f}" if max_safe is not None else "∞"
        lines.append(
            f"> 🟡 **Warning** — {headroom:.0f}% headroom before calamity threshold. "
            f"Max safe batch: ~{max_safe_str} PRs. Consider reducing release train size."
        )
    else:
        lines.append(
            f"> 🟢 **Healthy** — {headroom:.0f}% headroom. "
            f"Release train size is well within safe bounds."
        )
    lines.append("")

    if not rb["can_rollback"]:
        lines.append(f"> ⚠️ **Rollback trap**: {rb['reason']}")
        lines.append("")

    lines.append("**Deployment maturity breakdown:**")
    lines.append("")
    lines.append("| Dimension | Value | Weight | Contribution |")
    lines.append("|-----------|-------|--------|--------------|")
    for dim_name, dim_data in mat["dimensions"].items():
        label = dim_name.replace("_", " ").title()
        val = dim_data["value"]
        w = dim_data["weight"]
        contrib = dim_data["contribution"]
        bar_len = round(val * 10)
        bar = "█" * bar_len + "░" * (10 - bar_len)
        lines.append(f"| {label} | {bar} {val:.1f} | {w}× | {contrib:.1f} |")
    lines.append("")

    return lines


def _build_report(workspace: Path, pr_number: str, title: str) -> tuple[str, dict]:
    """Build markdown report from available MQ result files.

    Returns (markdown_text, context_vars_for_task_context).
    Section order: header → PR Overview → Review Findings → Gate Decision → Risk Score → Scope → Test Impact → Test Results → Lanes
    """
    ctx: dict[str, str] = {}
    # Named buckets assembled in desired display order at the end.
    header_sections: list[str] = []
    pr_overview_sections: list[str] = []  # PR title, issue, description (gate-review only)
    findings_sections: list[str] = []
    gate_sections: list[str] = []
    risk_sections: list[str] = []
    scope_sections: list[str] = []
    metrics_sections: list[str] = []  # single-PR 5-column dashboard
    test_sections: list[str] = []
    lane_sections: list[str] = []
    # Alias: most existing code appends to `sections`; we'll route by context below.
    sections = findings_sections  # default bucket reassigned per block

    heading = f"# {title}"
    if pr_number:
        heading += f" — PR #{pr_number}"
    header_sections.append(heading)
    header_sections.append("")

    # PR Overview — title, linked issue, description (problem/solution context).
    # Shown at top when scope.json has PR metadata (gate-review / scope-router mode).
    sections = pr_overview_sections
    scope = _read_json(workspace / "scope.json")
    if scope:
        pr_title = (scope.get("title") or "").strip()
        pr_body = (scope.get("body") or "").strip()
        issue_ref = scope.get("issue_ref") or {}
        if pr_title or pr_body or issue_ref:
            sections.append("## PR Overview")
            sections.append("")
            if pr_title:
                sections.append(f"**{pr_title}**")
                sections.append("")
            if issue_ref:
                key = issue_ref.get("key", "")
                url = issue_ref.get("url", "")
                issue_link = f"[{key}]({url})" if url else key
                if issue_link:
                    sections.append(f"**Linked Issue:** {issue_link}")
                    sections.append("")
            if pr_body:
                # Show full description (already truncated to 600 chars in scope_router)
                sections.append(pr_body)
                sections.append("")

    sections = scope_sections  # scope block
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
        additions = scope.get("additions", 0)
        deletions = scope.get("deletions", 0)
        if additions or deletions:
            sections.append(f"| LOC added | {additions} | Lines added in this PR |")
            sections.append(f"| LOC deleted | {deletions} | Lines deleted |")
        pr_author = scope.get("author", "")
        if pr_author:
            sections.append(f"| PR author | {pr_author} | |")
        pr_created = (scope.get("created_at") or scope.get("created_on") or "")[:10]
        if pr_created:
            sections.append(f"| PR date | {pr_created} | When PR was opened |")
        sections.append("")
        ctx["SCOPE"] = s
        ctx["BLAST_RADIUS"] = blast

    sections = risk_sections  # risk block
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
            sections.append("| Dimension | Score | Weight | Evidence |")
            sections.append("|-----------|-------|--------|----------|")
            for k, v in dims.items():
                label = k.replace("_", " ").title()
                w_raw = weights.get(k)
                w = f"{w_raw}×" if isinstance(w_raw, (int, float)) else str(w_raw or "")
                evidence = _dim_evidence(k, v, additions, deletions, n_files,
                                         scope, risk_scope, has_hist)
                sections.append(f"| {label} | {v}/10 | {w} | {evidence} |")
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

    # Single-PR metrics dashboard — only when scope.json exists (gate-review / scope mode).
    sections = metrics_sections
    if pr_number and scope:
        _add = scope.get("additions", 0)
        _del = scope.get("deletions", 0)
        _total_loc = _add + _del
        _n_files = scope.get("changed_files", 0)
        _blast = scope.get("blast_radius", "low")
        _cats = scope.get("categories", [])
        _pr_created = scope.get("created_at", "")

        _loc_sig = "🟢" if _total_loc <= 300 else ("🟡" if _total_loc <= 800 else "🔴")
        _files_sig = "🟢" if _n_files <= 5 else ("🟡" if _n_files <= 15 else "🔴")
        _blast_sig = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(_blast, "🟡")

        _risk_tier_val = risk.get("tier", "LOW") if risk else "—"
        _risk_score_val = risk.get("score", "—") if risk else "—"
        _risk_sig = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(_risk_tier_val, "🟡")

        sections.append("## Metrics Dashboard")
        sections.append("")
        sections.append("| Metric | Value | Benchmark | Signal | Description |")
        sections.append("|--------|-------|-----------|--------|-------------|")
        sections.append(f"| LOC Changed | {_total_loc} (+{_add}/−{_del}) | ≤300 healthy | {_loc_sig} | Total lines added + deleted |")
        sections.append(f"| Files Changed | {_n_files} | ≤5 healthy | {_files_sig} | Number of files modified |")
        sections.append(f"| Blast Radius | {_blast} | low ideal | {_blast_sig} | Scope of potential impact |")
        sections.append(f"| Risk Score | {_risk_score_val}/100 ({_risk_tier_val}) | <16 LOW | {_risk_sig} | Composite risk from 6 dimensions |")
        if _cats:
            sections.append(f"| File Categories | {', '.join(_cats[:4])} | — | — | Domain areas touched |")
        if _pr_created:
            try:
                from datetime import datetime as _dt, timezone as _tz
                _dt_val = _dt.fromisoformat(_pr_created.replace("Z", "+00:00"))
                _age_days = (_dt.now(tz=_tz.utc) - _dt_val).days
                _age_sig = "🟢" if _age_days < 3 else ("🟡" if _age_days < 14 else "🔴")
                sections.append(f"| PR Age | {_age_days}d | <3d fresh | {_age_sig} | Days since PR was opened |")
            except Exception:
                pass
        sections.append("")

    sections = test_sections  # test impact + results block
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

        # Slowest tests and health insights apply for any number of shards.
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

        _emit_test_health_insights(sections, shard_results, all_slow, passed, failed)

        ctx["TEST_STATUS"] = status
        ctx["TESTS_PASSED"] = str(passed)
        ctx["TESTS_FAILED"] = str(failed)

    sections = findings_sections  # review findings block
    review = _read_json(workspace / "review_result.json")
    if review:
        verdict = review.get("verdict", "UNKNOWN")
        all_findings = review.get("findings", [])
        verdict_emoji = {"DONE": "✅", "DONE_WITH_CONCERNS": "⚠️", "BLOCKED": "🚫"}.get(verdict, "📋")
        _SEV_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "ℹ️"}
        sev_counts: dict[str, int] = {}
        for f in all_findings:
            s = f.get("severity", "info").lower()
            sev_counts[s] = sev_counts.get(s, 0) + 1
        sev_summary = ", ".join(
            f"{c} {s}" for s, c in sorted(sev_counts.items(),
                                           key=lambda x: ["critical","high","medium","low","info"].index(x[0])
                                           if x[0] in ["critical","high","medium","low","info"] else 99)
        )
        sections.append("## Review Findings")
        sections.append("")
        if not all_findings:
            sections.append("✅ No issues found")
        else:
            sections.append(f"{verdict_emoji} **{verdict}** — {len(all_findings)} finding(s)"
                            + (f" ({sev_summary})" if sev_summary else ""))
        sections.append("")
        if all_findings:
            sections.append("| Severity | Category | File | Line | Summary |")
            sections.append("|----------|----------|------|------|---------|")
            for f in all_findings[:20]:
                sev = f.get("severity", "info").lower()
                cat = f.get("category", "")
                fpath = f.get("file", "")
                line = f.get("line", "")
                summary = f.get("summary", f.get("short_summary", ""))
                # Truncate long summaries for table readability
                if len(summary) > 80:
                    summary = summary[:77] + "..."
                sections.append(f"| {_SEV_EMOJI.get(sev,'')}{sev} | {cat} | `{fpath}` | {line} | {summary} |")
            if len(all_findings) > 20:
                sections.append(f"| ... | | | | {len(all_findings) - 20} more findings in review_result.json |")
            sections.append("")
        ctx["REVIEW_VERDICT"] = verdict
        ctx["REVIEW_FINDINGS_COUNT"] = str(len(all_findings))
        ctx["REVIEW_CRITICAL"] = str(sev_counts.get("critical", 0))
        ctx["REVIEW_HIGH"] = str(sev_counts.get("high", 0))

    sections = gate_sections  # gate decision block
    gate = _read_json(workspace / "gate_result.json")
    if gate:
        needs = gate.get("needs_approval", False)
        reason = gate.get("reason", "")
        risk_score_val = gate.get("risk_score", "?")
        risk_tier = gate.get("risk_tier", "?")
        has_critical = gate.get("has_critical_findings", False)
        findings_count = gate.get("findings_count", 0)
        emoji = "🚦" if needs else "✅"
        action = "**Human approval required** before merge" if needs else "**Safe to merge**"
        sections.append("## Gate Decision")
        sections.append("")
        sections.append(f"{emoji} {action}")
        sections.append("")
        sections.append("| Field | Value |")
        sections.append("|-------|-------|")
        sections.append(f"| Needs approval | {needs} |")
        sections.append(f"| Reason | {reason} |")
        sections.append(f"| Risk score | {risk_score_val} ({risk_tier}) |")
        sections.append(f"| Critical findings | {has_critical} |")
        sections.append(f"| Total findings | {findings_count} |")
        sections.append("")
        ctx["GATE_APPROVAL"] = str(needs).lower()
        ctx["GATE_REASON"] = reason

    sections = test_sections  # contract-test block
    contract = _read_json(workspace / "contract_test_summary.json")
    fuzz_detail = _read_json(workspace / "fuzz_result.json")
    if contract:
        c_status = contract.get("status", "?")
        c_iters = contract.get("fuzz_iterations", 0)
        c_findings = contract.get("fuzz_findings", 0)
        c_critical = contract.get("critical_findings", 0)
        c_breaking = contract.get("contract_breaking_changes", 0)
        c_endpoints_list = contract.get("endpoints", [])
        c_endpoints = contract.get("endpoints_scanned", len(c_endpoints_list))
        c_probes = contract.get("probe_types", [])
        c_probe_results = contract.get("probe_results", {})
        replay = contract.get("contract_replay", {})

        sections.append("## Contract + Fuzz Security Testing")
        sections.append("")

        # Overall status banner
        if c_status == "PASS":
            sections.append(f"✅ **PASS** — {c_endpoints} endpoints probed, "
                            f"{c_iters} security probes fired, 0 vulnerabilities found")
        else:
            sections.append(f"❌ **FAIL** — {c_critical} critical vulnerabilities detected "
                            f"across {c_endpoints} endpoints ({c_iters} probes, "
                            f"{c_findings} findings)")
        sections.append("")

        # AMS contract replay results
        ams_used = replay.get("ams_used", False)
        replay_passed = replay.get("succeeded", 0)
        # fall back to top-level contract_breaking_changes for pre-replay-block files
        replay_failed = replay.get("failed", c_breaking)
        sections.append("### Contract Replay (api-mock-service)")
        sections.append("")
        sections.append("Replays recorded HTTP interactions against the live service to detect "
                        "breaking changes — response shape, status code, or field regressions "
                        "that would break downstream consumers.")
        sections.append("")
        sections.append("| Metric | Value |")
        sections.append("|--------|-------|")
        sections.append(f"| AMS active | {'✅ yes' if ams_used else '❌ no'} |")
        sections.append(f"| Scenarios replayed | {replay_passed + replay_failed} |")
        sections.append(f"| Passed | {replay_passed} |")
        sections.append(f"| Breaking changes | {replay_failed} |")
        sections.append("")

        # Security probes breakdown
        sections.append("### Security Probe Results")
        sections.append("")
        sections.append("Each endpoint is probed with injection payloads that real attackers use. "
                        "A finding means the service returned a 5xx error, leaked SQL error text, "
                        "exposed a stack trace, or revealed credentials in the response body.")
        sections.append("")
        _PROBE_DESCS = {
            "SQLi": "SQL injection (`' OR '1'='1`) — detects unsanitised query parameters",
            "path-traversal": "Path traversal (`../../etc/passwd`) — detects unsanitised file paths",
            "XSS": "Cross-site scripting (`<script>alert(1)</script>`) — detects reflected input",
            "oversized-payload": "5 000-char payload — detects missing input length validation",
            "credential-exposure": "Response body scan for secrets / env-var leaks via diagnostic endpoints",
        }
        sections.append("| Probe type | What it tests | Findings |")
        sections.append("|------------|---------------|----------|")
        for ptype in (c_probes or list(_PROBE_DESCS.keys())):
            desc = _PROBE_DESCS.get(ptype, ptype)
            pr = c_probe_results.get(ptype, {})
            n = pr.get("count", 0)
            crit = pr.get("critical", 0)
            result = f"✅ 0" if n == 0 else f"⚠️ {n} ({crit} critical)"
            sections.append(f"| **{ptype}** | {desc} | {result} |")
        sections.append("")

        # Endpoint inventory
        if c_endpoints_list:
            sections.append("### Endpoints Tested")
            sections.append("")
            sections.append(f"Discovered {len(c_endpoints_list)} endpoints from recorded "
                            "API traffic (api-mock-service recording proxy):")
            sections.append("")
            sections.append("| Method | Path |")
            sections.append("|--------|------|")
            for ep in c_endpoints_list[:30]:
                sections.append(f"| `{ep.get('method','?')}` | `{ep.get('path','?')}` |")
            if len(c_endpoints_list) > 30:
                sections.append(f"| … | {len(c_endpoints_list) - 30} more endpoints |")
            sections.append("")

        # Detailed findings (if any)
        detail_findings = (fuzz_detail or {}).get("findings", [])
        if detail_findings:
            sections.append("### Findings Detail")
            sections.append("")
            sections.append("| Severity | Probe | HTTP Status | SQLi leak | Stack trace | Cred leak |")
            sections.append("|----------|-------|-------------|-----------|-------------|-----------|")
            for f in detail_findings[:20]:
                sev = f.get("severity", "medium")
                sev_emoji = "🔴" if sev == "critical" else "🟡"
                sections.append(
                    f"| {sev_emoji} {sev} | `{f.get('probe','')}` | {f.get('status','')} "
                    f"| {'✅' if f.get('sqli_leak') else '—'} "
                    f"| {'✅' if f.get('stack_leak') else '—'} "
                    f"| {'✅' if f.get('cred_leak') else '—'} |"
                )
            sections.append("")

        ctx["CONTRACT_STATUS"] = c_status
        ctx["CONTRACT_FINDINGS"] = str(c_findings)
        ctx["CONTRACT_CRITICAL"] = str(c_critical)

    # Read lane_groups.json once — used for both date range in Queue Status and lane processing below.
    lanes_data = _read_json(workspace / "lane_groups.json")

    sections = lane_sections  # merge queue analysis summary (read-only, no merges)
    queue_summary = _read_json(workspace / "queue_summary.json")
    if queue_summary:
        q_status = queue_summary.get("status", "UNKNOWN")
        q_repo = queue_summary.get("repo", "")
        q_lanes = queue_summary.get("lanes", 0)
        q_total = queue_summary.get("total_prs", 0)
        q_high = queue_summary.get("high_risk_prs", 0)
        q_needs_review = queue_summary.get("needs_human_review", 0)
        q_conflicts = queue_summary.get("conflict_lanes", 0)
        q_emoji = "✅" if q_high == 0 else ("🔴" if q_high > 2 else "⚠️")
        sections.append("## Queue Status")
        sections.append("")
        sections.append(f"{q_emoji} **{q_total} open PRs** across **{q_lanes} scope lanes**"
                        + (f" — repo: `{q_repo}`" if q_repo else ""))
        sections.append("")
        sections.append("| Metric | Value |")
        sections.append("|--------|-------|")
        sections.append(f"| Total open PRs | {q_total} |")
        sections.append(f"| Scope lanes | {q_lanes} |")
        sections.append(f"| High-risk PRs | {q_high} |")
        sections.append(f"| Needs human review | {q_needs_review} |")
        sections.append(f"| Conflict risk lanes | {q_conflicts} |")
        # Use already-loaded lanes data for date range (avoids double file read)
        if lanes_data:
            _early_prs = [p for l in lanes_data.get("lanes", []) for p in l.get("prs", [])]
            _dates = sorted(
                d[:10] for p in _early_prs
                for d in [(p.get("created_at") or p.get("created_on") or "")]
                if d
            )
            if len(_dates) >= 2:
                sections.append(f"| Date range | {_dates[0]} → {_dates[-1]} |")
            elif len(_dates) == 1:
                sections.append(f"| Date range | {_dates[0]} |")
        sections.append("")
        if q_needs_review:
            sections.append(f"> ⚠️ {q_needs_review} PR(s) flagged for human review before merge "
                            "(high blast-radius, failed CI, or sensitive paths detected).")
            sections.append("")
        if q_conflicts:
            sections.append(f"> ⚠️ {q_conflicts} lane(s) have conflict risk — "
                            "multiple PRs touching overlapping paths. Merge one at a time.")
            sections.append("")
        ctx["MQ_TOTAL"] = str(q_total)
        ctx["MQ_HIGH_RISK"] = str(q_high)
        ctx["MQ_NEEDS_REVIEW"] = str(q_needs_review)

    sections = lane_sections  # lanes block (grouping detail from group task)
    lanes = lanes_data
    ready = _read_json(workspace / "ready_prs.json")
    if lanes:
        lane_list = lanes.get("lanes", [])
        all_prs_flat = [p for l in lane_list for p in l.get("prs", [])]
        total_prs = len(all_prs_flat)

        # Queue health section (uses all PRs + lane hotspot metadata)
        voc = _valley_of_calm_section(all_prs_flat, lanes=lane_list)
        if voc:
            sections.append(voc)

        # Target branch filter acknowledgement
        if ready:
            tbf = (ready.get("target_branch_filter") or "").strip()
            if tbf:
                sections.append(f"> Analysis scoped to PRs targeting: **{tbf}**")
                sections.append("")

        sections.append("## Merge Queue Lanes")
        sections.append("")

        # Detect CI unavailability: BB API does not return CI status in bulk PR list
        all_ci = [p.get("ci_status", "unknown") for p in all_prs_flat]
        ci_unavailable = all_ci and all(s in ("unknown",) for s in all_ci)

        if ci_unavailable:
            sections.append(
                "> ℹ️ **CI status: N/A** — Bitbucket REST API does not return pipeline status "
                "in the bulk PR list endpoint. CI column shows `N/A` throughout."
            )
            sections.append("")

        def _ci_cell(ci_status: str) -> str:
            if ci_unavailable:
                return "N/A"
            if ci_status == "pass":
                return "✅"
            if ci_status == "fail":
                return "❌"
            if ci_status == "pending":
                return "⏳"
            return "—"

        def _age_label(age_hours: float) -> str:
            if age_hours < 24:
                return f"{age_hours:.0f}h"
            days = age_hours / 24
            if days >= 60:
                return f"⛔ {days:.0f}d"
            if days >= 30:
                return f"🔴 {days:.0f}d"
            if days >= 14:
                return f"🟡 {days:.0f}d"
            return f"{days:.0f}d"

        def _issue_cell(issue_ref: dict | None) -> str:
            if not issue_ref:
                return "—"
            key = issue_ref.get("key", "")
            url = issue_ref.get("url", "")
            return f"[{key}]({url})" if url else key

        def _rev_cell(approval_count: int, reviewer_count: int) -> str:
            """Reviewer/approval cell.

            BB bulk API does not return approval status (participants not in bulk endpoint).
            Show approvals/reviewers only when approval_count > 0 (has actual data).
            Otherwise show assigned reviewer count to avoid misleading '0/N ✅'.
            """
            if approval_count > 0:
                return f"{approval_count}/{reviewer_count} ✅"
            if reviewer_count > 0:
                return f"{reviewer_count} assigned"
            return "—"

        def _per_pr_table(prs_in_tier: list[dict], sec: list[str]) -> None:
            """Emit per-PR detail table for one risk tier."""
            sec.append("| Status | PR | Title | Category | Type | Blast | Risk | LOC | Files | CI | Age | Reviewers | Issues |")
            sec.append("|--------|-----|-------|----------|------|-------|------|-----|-------|----|-----|-----------|--------|")
            for p in prs_in_tier:
                pr_link = _pr_link(p)
                title = (p.get("title") or "")[:80]
                cat = p.get("category", "unknown")
                conf = p.get("category_confidence", "")
                cat_cell = f"{cat}*" if conf not in ("file_path", "label", "") else cat
                if cat in ("security", "authn_authz"):
                    cat_cell = f"⚠️ {cat_cell}"
                if p.get("is_hotspot"):
                    cat_cell = f"🔥 {cat_cell}"
                pt = p.get("pr_type", "unknown")
                type_emoji = _PR_TYPE_EMOJI.get(pt, "❓")
                if p.get("is_wip_pr"):
                    type_emoji += "🚧"
                elif p.get("is_docs_pr"):
                    type_emoji += "📝"
                blast = p.get("blast_radius", "low")
                blast_cell = f"{_RISK_EMOJI.get(blast, '⚪')} {blast}"
                risk_score = p.get("risk_score", 0)
                risk_tier = p.get("risk_tier", blast)
                risk_cell = f"{_RISK_EMOJI.get(risk_tier, '⚪')} {risk_score:.0f}"
                loc = p.get("total_loc", 0)
                loc_cell = f"{loc:,}" if loc else "—"
                file_count = p.get("file_count", 0)
                files_cell = str(file_count) if file_count else "—"
                ci_cell = _ci_cell(p.get("ci_status", "unknown"))
                age = _age_label(p.get("age_hours", 0))
                approvals = p.get("approval_count", 0)
                reviewers = p.get("reviewer_count", 0)
                rev_cell = _rev_cell(approvals, reviewers)
                issue_cell = _issue_cell(p.get("issue_ref"))
                status = _format_pr_status(p).split(" ", 1)[1] if " " in _format_pr_status(p) else _format_pr_status(p)
                sec.append(
                    f"| {status} | {pr_link} | {title} | {cat_cell} | {type_emoji} | {blast_cell} "
                    f"| {risk_cell} | {loc_cell} | {files_cell} | {ci_cell} | {age} | {rev_cell} | {issue_cell} |"
                )
            if len(prs_in_tier) > 20:
                sec.append(f"_+{len(prs_in_tier) - 20} more — use `--target-branch` to narrow scope_")
            sec.append("")

        # Group canonical lanes by branch for hierarchical display
        # canonical lane_id = "{branch}/{risk_tier}"
        canonical_lanes = [l for l in lane_list if not l.get("lane_id", "").startswith("stacked/")]
        stacked_lanes = [l for l in lane_list if l.get("lane_id", "").startswith("stacked/")]

        # Group canonical lanes by branch
        branch_lanes: dict[str, dict[str, list[dict]]] = {}
        for lane in canonical_lanes:
            lid = lane.get("lane_id", "")
            if "/" not in lid:
                continue
            rest, risk_tier = lid.rsplit("/", 1)
            branch = rest
            if branch not in branch_lanes:
                branch_lanes[branch] = {}
            branch_lanes[branch][risk_tier] = lane.get("prs", [])

        # Sort branches by total PR count descending
        branch_order = sorted(branch_lanes, key=lambda b: -sum(len(v) for v in branch_lanes[b].values()))

        # Summary line: branch breakdown
        branch_summary = " | ".join(
            f"{b}: {sum(len(v) for v in branch_lanes[b].values())} PRs"
            for b in branch_order
        )
        stacked_total = sum(l.get("pr_count", 0) for l in stacked_lanes)
        if stacked_total:
            branch_summary += f" | stacked: {stacked_total} PRs"
        sections.append(f"**{len(lane_list)} lanes** — {branch_summary}")
        sections.append("")

        for branch in branch_order:
            tier_prs = branch_lanes[branch]
            branch_total = sum(len(v) for v in tier_prs.values())
            n_high = len(tier_prs.get("high", []))
            n_med = len(tier_prs.get("medium", []))
            n_low = len(tier_prs.get("low", []))
            risk_parts = []
            if n_high:
                risk_parts.append(f"🔴 {n_high} high")
            if n_med:
                risk_parts.append(f"🟡 {n_med} medium")
            if n_low:
                risk_parts.append(f"🟢 {n_low} low")
            risk_str = ", ".join(risk_parts) or "🟢 all low"
            sections.append(f"### Branch: {branch} ({branch_total} PRs — {risk_str})")
            sections.append("")

            for tier in _RISK_TIER_ORDER:
                prs_in_tier = tier_prs.get(tier, [])
                if not prs_in_tier:
                    continue
                emoji = _RISK_EMOJI.get(tier, "⚪")
                tier_label = tier.title()
                sections.append(f"#### {emoji} {tier_label} Risk ({len(prs_in_tier)} PRs)")
                sections.append("")
                _per_pr_table(prs_in_tier, sections)

        if stacked_lanes:
            sections.append(f"### Stacked PRs ({stacked_total} PRs targeting feature branches)")
            sections.append("")
            sections.append(
                "> ⚠️ These PRs target feature branches — they depend on those branches being "
                "merged first. Do not batch with canonical branch lanes."
            )
            sections.append("")
            for lane in stacked_lanes:
                feature = lane.get("lane_id", "").replace("stacked/", "", 1)
                prs_in_lane = lane.get("prs", [])
                n_high = sum(1 for p in prs_in_lane if p.get("risk_tier", p.get("blast_radius")) == "high")
                n_med = sum(1 for p in prs_in_lane if p.get("risk_tier", p.get("blast_radius")) == "medium")
                sections.append(f"**→ {feature}** ({len(prs_in_lane)} PRs)")
                sections.append("")
                sections.append("| PR | Title | Target | Category | Type | Blast | Risk | LOC | Cx | CI | Age | Reviewers | Issues |")
                sections.append("|----|-------|--------|----------|------|-------|------|-----|-----|-----|-----|-----------|--------|")
                for p in prs_in_lane:
                    pr_link = _pr_link(p)
                    title = (p.get("title") or "")[:40]
                    target = p.get("target_branch", feature)
                    cat = p.get("category", "unknown")
                    if p.get("is_hotspot"):
                        cat = f"🔥 {cat}"
                    pt = p.get("pr_type", "unknown")
                    type_emoji = _PR_TYPE_EMOJI.get(pt, "❓")
                    blast = p.get("blast_radius", "low")
                    blast_cell = f"{_RISK_EMOJI.get(blast,'⚪')} {blast}"
                    risk_score = p.get("risk_score", 0)
                    risk_tier = p.get("risk_tier", blast)
                    risk_cell = f"{_RISK_EMOJI.get(risk_tier, '⚪')} {risk_score:.0f}"
                    loc = p.get("total_loc", 0)
                    loc_cell = f"{loc:,}" if loc else "—"
                    cx = p.get("complexity", "low")
                    cx_cell = _RISK_EMOJI.get(cx, "—")
                    ci_cell = _ci_cell(p.get("ci_status", "unknown"))
                    age = _age_label(p.get("age_hours", 0))
                    approvals = p.get("approval_count", 0)
                    reviewers = p.get("reviewer_count", 0)
                    rev_cell = _rev_cell(approvals, reviewers)
                    issue_cell = _issue_cell(p.get("issue_ref"))
                    sections.append(
                        f"| {pr_link} | {title} | {target} | {cat} | {type_emoji} | {blast_cell} "
                        f"| {risk_cell} | {loc_cell} | {cx_cell} | {ci_cell} | {age} | {rev_cell} | {issue_cell} |"
                    )
                sections.append("")

        ctx["LANE_COUNT"] = str(len(lane_list))
        ctx["QUEUED_PRS"] = str(total_prs)

    ordered = (
        header_sections
        + pr_overview_sections
        + findings_sections
        + gate_sections
        + risk_sections
        + scope_sections
        + metrics_sections
        + test_sections
        + lane_sections
    )
    return "\n".join(ordered), ctx


def _generate_risk_heatmap_html(workspace: Path, reports_dir: Path) -> None:
    """Generate a standalone HTML risk heatmap showing batch size vs defect rate.

    Produces reports/risk_heatmap.html with a color-coded grid and current
    position marker. No JS dependencies — pure inline CSS/HTML.
    """
    from scripts.mq.simulate import merge_batch_success, load_deployment_profile

    prs_file = workspace / "ready_prs.json"
    if not prs_file.exists():
        return

    try:
        prs = json.loads(prs_file.read_text())
    except Exception:
        return
    if not prs:
        return

    total = len(prs)
    type_counts: dict[str, int] = {}
    for p in prs:
        t = p.get("pr_type", "unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
    n_defect = type_counts.get("bug", 0) + type_counts.get("security", 0)
    actual_defect_rate = n_defect / total if total else 0.0

    lane_file = workspace / "lane_groups.json"
    if lane_file.exists():
        try:
            lane_data = json.loads(lane_file.read_text())
            lane_list = lane_data.get("lanes", []) if isinstance(lane_data, dict) else lane_data
            sizes = [len(l.get("prs", [])) for l in lane_list if isinstance(l, dict) and l.get("prs")]
            avg_batch = sum(sizes) / len(sizes) if sizes else total
        except Exception:
            avg_batch = total
    else:
        avg_batch = total

    profile_name = os.environ.get("DEPLOYMENT_PROFILE", "").strip()
    prs_per_release = int(avg_batch)
    if profile_name:
        profile = load_deployment_profile(profile_name, queue_size=total)
        if profile:
            prs_per_release = profile.get("prs_per_release", int(avg_batch))

    defect_rates = [0.005, 0.01, 0.02, 0.03, 0.05, 0.07, 0.10, 0.15, 0.20]
    batch_sizes = [1, 3, 5, 10, 15, 20, 30, 50, 75, 100]

    rows_html = []
    for dr in defect_rates:
        dr_pct = dr * 100
        dr_label = f"{dr_pct:.1f}%"
        is_current_row = abs(dr - actual_defect_rate) <= 0.015
        cells = []
        for bs in batch_sizes:
            success = merge_batch_success(dr, bs)
            pct = round(success * 100, 1)
            if success >= 0.90:
                bg = "#2ea043"
            elif success >= 0.70:
                bg = "#d4a017"
            elif success >= 0.50:
                bg = "#e3822a"
            else:
                bg = "#cf222e"
            is_current = (
                is_current_row
                and abs(bs - avg_batch) <= max(5, avg_batch * 0.3)
            )
            border = "3px solid #0969da" if is_current else "1px solid #30363d"
            marker = " ★" if is_current else ""
            tooltip = (
                f"Defect rate {dr_pct:.1f}% means {dr_pct:.1f} out of 100 PRs are buggy. "
                f"Deploying {bs} PRs at once → {pct}% chance all are clean."
            )
            cells.append(
                f'<td title="{tooltip}" style="background:{bg};color:#fff;border:{border};'
                f'text-align:center;padding:6px;font-size:13px;min-width:55px;cursor:help">'
                f'{pct}%{marker}</td>'
            )
        row_bg = "#1c2333" if is_current_row else "#161b22"
        row_extra = "font-size:14px;" if is_current_row else ""
        row_tip = f"{dr_pct:.1f}% of PRs in the queue are bug/security fixes"
        rows_html.append(
            f'<tr><td title="{row_tip}" style="padding:6px;font-weight:bold;background:{row_bg};'
            f'color:#c9d1d9;text-align:right;{row_extra}cursor:help">'
            f'{dr_label}{"  ◄" if is_current_row else ""}</td>{"".join(cells)}</tr>'
        )

    header_cells = ""
    for bs in batch_sizes:
        is_cur_col = abs(bs - avg_batch) <= max(5, avg_batch * 0.3)
        col_tip = f"Deploy {bs} PRs together in one release batch"
        col_bg = "#1c2333" if is_cur_col else "#161b22"
        col_marker = " ▼" if is_cur_col else ""
        header_cells += (
            f'<th title="{col_tip}" style="padding:6px;background:{col_bg};'
            f'color:#c9d1d9;min-width:55px;cursor:help">{bs}{col_marker}</th>'
        )

    summary_lines = [
        f"Queue size: {total} PRs",
        f"Defect-proxy rate: {actual_defect_rate*100:.1f}%",
        f"Avg batch (lane) size: {avg_batch:.0f}",
        f"PRs/release: {prs_per_release}",
        f"Current merge success: {merge_batch_success(actual_defect_rate, avg_batch)*100:.1f}%",
    ]

    import html as _html
    type_breakdown = _html.escape(" | ".join(f"{k}: {v}" for k, v in sorted(type_counts.items(), key=lambda x: -x[1])[:8]))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Merge Queue Risk Heatmap</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0d1117; color: #c9d1d9; max-width: 1100px; margin: 40px auto; padding: 0 20px; }}
  h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
  h2 {{ color: #79c0ff; margin-top: 28px; }}
  table {{ border-collapse: collapse; margin: 16px 0; }}
  .summary {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px;
              padding: 16px; margin: 16px 0; }}
  .summary p {{ margin: 4px 0; }}
  .legend {{ display: flex; gap: 16px; margin: 12px 0; flex-wrap: wrap; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; }}
  .legend-box {{ width: 20px; height: 20px; border-radius: 3px; }}
  .current-marker {{ border: 3px solid #0969da; display: inline-block; width: 16px; height: 16px; border-radius: 3px; }}
  .type-bar {{ display: flex; height: 28px; border-radius: 4px; overflow: hidden; margin: 8px 0; }}
  .type-bar div {{ display: flex; align-items: center; justify-content: center;
                   font-size: 11px; color: #fff; white-space: nowrap; overflow: hidden; }}
</style>
</head>
<body>
<h1>Merge Queue Risk Heatmap</h1>

<div class="summary">
  {"<br>".join(f"<p>{line}</p>" for line in summary_lines)}
  <p style="font-size:12px;color:#8b949e;margin-top:8px">Type breakdown: {type_breakdown}</p>
</div>

<div class="legend">
  <div class="legend-item"><div class="legend-box" style="background:#2ea043"></div> &ge;90% success</div>
  <div class="legend-item"><div class="legend-box" style="background:#d4a017"></div> 70–89%</div>
  <div class="legend-item"><div class="legend-box" style="background:#e3822a"></div> 50–69%</div>
  <div class="legend-item"><div class="legend-box" style="background:#cf222e"></div> &lt;50%</div>
  <div class="legend-item"><div class="current-marker"></div> Your position</div>
</div>

<h2>Batch Success Rate: Defect Rate × Batch Size</h2>
<p style="font-size:13px;color:#8b949e"><strong>Rows</strong> = defect rate: % of PRs that are bug/security fixes (e.g., 5% means 5 out of 100 PRs are buggy).<br>
<strong>Columns</strong> = batch size: how many PRs are released together in one deploy.<br>
<strong>Cells</strong> = probability that ALL PRs in the batch are defect-free. Formula: (1 − defect_rate)<sup>batch_size</sup></p>

<table>
  <thead>
    <tr>
      <th style="padding:6px;background:#161b22;color:#8b949e" title="% of PRs in queue that are bug/security fixes">Defect Rate ↓ \\ Batch →</th>
      {header_cells}
    </tr>
  </thead>
  <tbody>
    {"".join(rows_html)}
  </tbody>
</table>

<h2>Reading the Heatmap</h2>
<ul style="line-height:1.8">
  <li><strong>Defect rate (rows)</strong> — % of PRs in the queue that are bug-fix or security PRs. E.g., 5.0% means 5 out of 100 PRs are buggy. This is a proxy for the probability that any given PR introduces a defect.</li>
  <li><strong>Batch size (columns)</strong> — how many PRs are released together in one deploy/release train. E.g., "10" means 10 PRs ship at once. Smaller batches = lower risk per deploy.</li>
  <li><strong>Cell value</strong> — probability that ALL PRs in the batch are defect-free. E.g., "82%" means 82% chance the entire batch is clean (18% chance at least one PR is buggy).</li>
  <li><strong>Green cells (&ge;90%)</strong> — safe zone. Small batches or low defect rates.</li>
  <li><strong>Yellow cells (70–89%)</strong> — caution. 10–30% chance of a bad batch.</li>
  <li><strong>Orange cells (50–69%)</strong> — danger. Coin-flip whether your batch is clean.</li>
  <li><strong>Red cells (&lt;50%)</strong> — calamity zone. More likely to fail than succeed.</li>
  <li><strong>Blue border (★)</strong> — your current position. <strong>◄</strong> marks your defect rate row, <strong>▼</strong> marks your batch size column.</li>
</ul>

<p style="font-size:12px;color:#484f58;margin-top:32px">
  Generated from merge queue analysis. Model: Joe Magerramov's deployment risk model.
</p>
</body>
</html>"""

    if write_report(reports_dir / "risk_heatmap.html", html):
        print("[mq-report] reports/risk_heatmap.html written", flush=True)


def _build_mq_slack_summary(ctx: dict, all_prs: list[dict], lanes: list[dict]) -> str:
    """Build a condensed Slack summary for the merge queue report (~800 chars)."""
    total = int(ctx.get("MQ_TOTAL", len(all_prs)))
    high_risk = int(ctx.get("MQ_HIGH_RISK", 0))
    needs_review = int(ctx.get("MQ_NEEDS_REVIEW", 0))

    dates: list[str] = []
    for p in all_prs:
        d = p.get("created_at") or p.get("created_on") or ""
        if d:
            dates.append(d[:10])
    if dates:
        dates.sort()
        date_range = f"{dates[0]} → {dates[-1]}"
    else:
        date_range = ""

    conflict_lanes = int(ctx.get("MQ_CONFLICT_LANES", 0))
    if not conflict_lanes and lanes:
        conflict_lanes = sum(1 for ln in lanes if ln.get("conflict_risk"))

    if high_risk == 0:
        risk_signal = "✅ low risk"
    elif high_risk <= 2:
        risk_signal = f"⚠️ {high_risk} high-risk"
    else:
        risk_signal = f"🔴 {high_risk} high-risk"

    lines_out: list[str] = []
    header = f"*Merge Queue* — {date_range}" if date_range else "*Merge Queue*"
    lines_out.append(header)

    # Queue health line
    stale_7d = [p for p in all_prs if _compute_pr_age_inline(p) > 7]
    stale_14d = [p for p in all_prs if _compute_pr_age_inline(p) > 14]
    blast_counts: dict = Counter(p.get("blast_radius", "low") for p in all_prs)
    high_blast = blast_counts.get("high", 0) + blast_counts.get("critical", 0)
    hotspot_count = sum(1 for p in all_prs if p.get("is_hotspot"))

    queue_parts = [f"{total} open PRs", risk_signal]
    if high_blast:
        queue_parts.append(f"🔴 {high_blast} high-blast")
    if hotspot_count:
        queue_parts.append(f"🔥 {hotspot_count} hotspot paths")
    lines_out.append("*Queue Health*: " + " · ".join(queue_parts))

    # Review + stale + CI line
    ci_known = [p for p in all_prs if p.get("ci_status") not in ("unknown", None, "")]
    ci_parts: list[str] = []
    if ci_known:
        ci_pass = sum(1 for p in ci_known if p.get("ci_status") == "pass")
        ci_pct = round(ci_pass / len(ci_known) * 100, 1)
        ci_emoji = "✅" if ci_pct >= 90 else ("⚠️" if ci_pct >= 75 else "🔴")
        ci_parts.append(f"CI: {ci_pass}/{len(ci_known)} ({ci_pct}%) {ci_emoji}")
    else:
        ci_parts.append("CI: N/A")

    reviewable = [p for p in all_prs if not p.get("is_bot_authored")]
    if reviewable:
        with_review = sum(1 for p in reviewable if p.get("has_substantive_review"))
        rev_pct = round(with_review / len(reviewable) * 100, 1)
        rev_emoji = "✅" if rev_pct >= 80 else ("⚠️" if rev_pct >= 50 else "🔴")
        ci_parts.insert(0, f"Review: {rev_pct}% {rev_emoji}")

    if stale_7d:
        stale_emoji = "🔴" if len(stale_7d) > len(all_prs) * 0.2 else "⚠️"
        ci_parts.append(f"{stale_emoji} {len(stale_7d)} stale (>7d)")

    lines_out.append("*Review*: " + " · ".join(ci_parts))

    # Risk line
    risk_parts: list[str] = []
    if needs_review:
        risk_parts.append(f"⚠️ {needs_review} need human review")
    if conflict_lanes:
        risk_parts.append(f"🔀 {conflict_lanes} conflict lane(s)")
    if stale_14d:
        risk_parts.append(f"🕐 {len(stale_14d)} stale (>14d)")
    if risk_parts:
        lines_out.append("*Risk*: " + " · ".join(risk_parts))

    # Work type distribution
    types = Counter(p.get("pr_type", "unknown") for p in all_prs)
    if types:
        top = types.most_common(4)
        type_str = " · ".join(f"{t}:{n}" for t, n in top)
        lines_out.append(f"*Work*: {type_str}")

    # Defect rate + batch success
    bug_sec_prs = [p for p in all_prs if p.get("pr_type") in ("bug", "security")]
    defect_parts: list[str] = []
    if all_prs:
        n_defect = len(bug_sec_prs)
        defect_pct = round(n_defect / len(all_prs) * 100, 1)
        defect_emoji = "✅" if defect_pct <= 8 else ("⚠️" if defect_pct <= 15 else "🔴")
        defect_parts.append(f"Defect rate: {defect_pct}% ({n_defect}/{total}) {defect_emoji}")
        # Batch success estimate — compute from lanes
        n_lanes = max(len(lanes), 1)
        avg_batch = round(total / n_lanes, 1)
        if n_defect > 0 and avg_batch > 0:
            from scripts.mq.simulate import merge_batch_success
            batch_success = round(merge_batch_success(n_defect / total, avg_batch) * 100, 1)
            batch_emoji = "✅" if batch_success >= 90 else ("⚠️" if batch_success >= 70 else "🔴")
            defect_parts.append(f"Batch success: {batch_success}% (at ~{avg_batch:.0f} PRs/batch) {batch_emoji}")
    if defect_parts:
        lines_out.append("*Deploy Risk*: " + " · ".join(defect_parts))

    # Throughput: avg age + CFR proxy
    throughput_parts: list[str] = []
    if all_prs:
        ages = [_compute_pr_age_inline(p) for p in all_prs]
        avg_age = round(sum(ages) / len(ages), 1) if ages else 0
        throughput_parts.append(f"{avg_age}d avg age")
        cfr = round(len(bug_sec_prs) / len(all_prs) * 100, 1)
        cfr_emoji = "✅" if cfr <= 10 else ("⚠️" if cfr <= 25 else "🔴")
        throughput_parts.append(f"CFR proxy: {cfr}% {cfr_emoji}")
    if throughput_parts:
        lines_out.append("*Throughput*: " + " · ".join(throughput_parts))

    # Top categories (compact)
    categories = Counter(p.get("category", "unknown") for p in all_prs)
    if categories:
        top_cats = categories.most_common(5)
        cat_str = " · ".join(f"{c}:{n}" for c, n in top_cats)
        lines_out.append(f"*Categories*: {cat_str}")

    lines_out.append("Full report in thread ↑")
    return "\n".join(lines_out)


def main() -> None:
    config = load_config(required=[])
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    # reports/ is created by the YAML script (mkdir -p) before Python runs, as UID=1000.
    # mkdir here is a safe no-op guard for local runs outside Formicary.
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    from scripts.mq._shared import parse_pr_ref
    pr_number, _ = parse_pr_ref(config.get("PR_NUMBER", ""))
    title = config.get("REPORT_TITLE", "PR Review Report")

    report_text, ctx = _build_report(workspace, pr_number, title)

    print("\n" + "=" * 60, flush=True)
    print(report_text, flush=True)
    print("=" * 60 + "\n", flush=True)

    write_report(reports_dir / "report.md", report_text)
    print("[mq-report] reports/report.md written", flush=True)

    for key, val in ctx.items():
        print(f"::add-task-context {key}::{val}", flush=True)

    from scripts.common.report_renderer import render_simple_html
    try:
        html = render_simple_html(title, report_text)
        if write_report(reports_dir / "report.html", html):
            print("[mq-report] reports/report.html written", flush=True)
    except Exception as e:
        print(f"[mq-report] HTML render failed (non-fatal): {e}", flush=True)

    try:
        _generate_risk_heatmap_html(workspace, reports_dir)
    except Exception as e:
        print(f"[mq-report] risk heatmap generation failed (non-fatal): {e}", flush=True)

    _slack_lanes_raw = _read_json(workspace / "lane_groups.json")
    _slack_all_prs: list[dict] = []
    if _slack_lanes_raw:
        _slack_all_prs = [p for ln in _slack_lanes_raw.get("lanes", []) for p in ln.get("prs", [])]
    if _slack_all_prs:
        slack_text = _build_mq_slack_summary(ctx, _slack_all_prs, _slack_lanes_raw.get("lanes", []))
    else:
        slack_text = format_for_slack(report_text)

    thread_ts = config.get("SLACK_THREAD_TS") or config.get("SlackThreadTs") or None
    slack_ok = post_report(config, slack_text, report_text,
                           title=title, filename="report.html",
                           thread_ts=thread_ts, task_type=config.get("TASK_TYPE", "report"))

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
