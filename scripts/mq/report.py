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


def _build_report(workspace: Path, pr_number: str, title: str) -> tuple[str, dict]:
    """Build markdown report from available MQ result files.

    Returns (markdown_text, context_vars_for_task_context).
    Section order: header → Review Findings → Gate Decision → Risk Score → Scope → Test Impact → Test Results → Lanes
    """
    ctx: dict[str, str] = {}
    # Named buckets assembled in desired display order at the end.
    header_sections: list[str] = []
    findings_sections: list[str] = []
    gate_sections: list[str] = []
    risk_sections: list[str] = []
    scope_sections: list[str] = []
    test_sections: list[str] = []
    lane_sections: list[str] = []
    # Alias: most existing code appends to `sections`; we'll route by context below.
    sections = findings_sections  # default bucket reassigned per block

    heading = f"# {title}"
    if pr_number:
        heading += f" — PR #{pr_number}"
    header_sections.append(heading)
    header_sections.append("")

    sections = scope_sections  # scope block
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
    if contract:
        c_status = contract.get("status", "?")
        c_iters = contract.get("fuzz_iterations", 0)
        c_findings = contract.get("fuzz_findings", 0)
        c_critical = contract.get("critical_findings", 0)
        c_breaking = contract.get("contract_breaking_changes", 0)
        status_emoji = ":white_check_mark:" if c_status == "PASS" else ":x:"
        sections.append("## Contract + Fuzz Results")
        sections.append("")
        sections.append("| Field | Value |")
        sections.append("|-------|-------|")
        sections.append(f"| Status | {status_emoji} **{c_status}** |")
        sections.append(f"| Fuzz iterations | {c_iters} |")
        sections.append(f"| Security findings | {c_findings} ({c_critical} critical) |")
        sections.append(f"| Contract breaking changes | {c_breaking} |")
        sections.append("")
        ctx["CONTRACT_STATUS"] = c_status
        ctx["CONTRACT_FINDINGS"] = str(c_findings)
        ctx["CONTRACT_CRITICAL"] = str(c_critical)

    sections = lane_sections  # lanes block
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

    ordered = (
        header_sections
        + findings_sections
        + gate_sections
        + risk_sections
        + scope_sections
        + test_sections
        + lane_sections
    )
    return "\n".join(ordered), ctx


def main() -> None:
    config = load_config(required=[])
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    from scripts.mq._shared import parse_pr_ref
    pr_number, _ = parse_pr_ref(config.get("PR_NUMBER", ""))
    title = config.get("REPORT_TITLE", "PR Review Report")

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
