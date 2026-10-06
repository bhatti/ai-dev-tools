"""Post PR audit report to Slack.

Reads reports/pr_audit_report.md and reports/pr_audit_findings.json from WORKSPACE_DIR.
Posts a concise digest to Slack (trimming verbose sections like Per-PR Metrics,
full evidence blocks, and "Checked — No Issues Found") while attaching the full
HTML/MD report as an artifact.

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

# Sections to DROP from the Slack digest (case-insensitive heading match).
# These remain in the HTML/MD artifact.
_DROP_SECTIONS = [
    "pre-computed pr metrics",
    "per-pr metrics",
    "checked",
    "claude-assessed process metrics",
]

# Metric keys and their display names, benchmarks, and "higher is better" flag.
# Used to build the compact metrics block in the Slack digest.
_KEY_METRICS: list[tuple[str, str, str, bool]] = [
    ("rubber_stamp_rate", "Rubber-stamp", "≤10%", False),
    ("security_review_invocation_rate", "Security review", "100%", True),
    ("xl_pr_review_coverage_pct", "XL PR review", "100%", True),
    ("spec_coverage_pct", "Spec coverage", "≥80%", True),
    ("code_review_skill_catch_rate", "Review catch", ">60%", True),
    ("bot_finding_follow_through_rate", "Bot follow-through", ">90%", True),
    ("human_review_burden", "Human burden", "<40%", False),
    ("large_pr_review_depth", "Large PR depth", ">5", True),
    ("revert_followup_rate", "Revert rate", "<5%", True),  # higher_is_better=True means low is good here via inversion
]


def _metric_signal(key: str, val: float) -> str:
    """Return signal emoji for a metric value."""
    if key in ("spec_coverage_pct", "ci_catch_rate", "code_review_skill_catch_rate",
               "bot_finding_follow_through_rate", "security_review_invocation_rate"):
        return ":large_green_circle:" if val >= 80 else (":large_yellow_circle:" if val >= 60 else ":red_circle:")
    if key == "large_pr_review_depth":
        return ":large_green_circle:" if val >= 5 else (":large_yellow_circle:" if val >= 2 else ":red_circle:")
    if key in ("human_review_burden", "rubber_stamp_rate"):
        return ":large_green_circle:" if val <= 10 else (":large_yellow_circle:" if val <= 30 else ":red_circle:")
    if key == "revert_followup_rate":
        return ":large_green_circle:" if val <= 5 else (":large_yellow_circle:" if val <= 15 else ":red_circle:")
    if key == "xl_pr_review_coverage_pct":
        return ":large_green_circle:" if val >= 100 else (":large_yellow_circle:" if val >= 80 else ":red_circle:")
    return ":white_circle:"


def _build_slack_digest(findings: dict, report_md: str) -> str:
    """Build a concise Slack digest from structured findings and the markdown report.

    Keeps: executive summary, findings (trimmed), skills assessment, key metrics,
    recommended skill updates, positive patterns (trimmed).
    Drops: per-PR tables, full evidence blocks, "checked no issues", Claude-assessed
    metrics (redundant with compact metrics block).
    """
    lines: list[str] = []

    # --- Executive Summary (extract from markdown, first ~500 chars) ---
    exec_match = re.search(
        r"(?:^|\n)#{1,3}\s+Executive\s+Summary\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
        report_md, re.DOTALL | re.IGNORECASE,
    )
    if exec_match:
        exec_text = exec_match.group(1).strip()
        if len(exec_text) > 600:
            exec_text = exec_text[:600].rsplit(". ", 1)[0] + "."
        lines.append(exec_text)
        lines.append("")

    # --- Findings (from structured JSON — compact one-liner per finding) ---
    findings_list = findings.get("findings", [])
    if findings_list:
        shipped = [f for f in findings_list if f.get("pr_state") == "merged"]
        in_flight = [f for f in findings_list if f.get("pr_state") != "merged"]

        if shipped:
            lines.append("*Shipped Gaps (Merged)*")
            for f in sorted(shipped, key=lambda x: _sev_rank(x.get("severity", "LOW"))):
                prs = ", ".join(f"#{p}" for p in (f.get("prs") or [f.get("pr_number", "")])) if f.get("prs") or f.get("pr_number") else ""
                lines.append(f"• [{f.get('severity', '?')}] {f.get('title', '?')}" + (f" — {prs}" if prs else ""))
            lines.append("")

        if in_flight:
            lines.append("*In-Flight Concerns (Open)*")
            for f in sorted(in_flight, key=lambda x: _sev_rank(x.get("severity", "LOW"))):
                prs = ", ".join(f"#{p}" for p in (f.get("prs") or [f.get("pr_number", "")])) if f.get("prs") or f.get("pr_number") else ""
                lines.append(f"• [{f.get('severity', '?')}] {f.get('title', '?')}" + (f" — {prs}" if prs else ""))
            lines.append("")
    else:
        _append_trimmed_sections(lines, report_md)

    # --- Skills Assessment (from JSON or markdown) ---
    skills = findings.get("skills_assessment", {})
    if skills:
        parts = []
        for area in ("coding", "review", "testing", "sre", "security", "architecture"):
            rating = skills.get(area, "")
            if rating:
                parts.append(f"{area.title()}: {rating}")
        if parts:
            lines.append("*Skills*")
            lines.append("• " + " · ".join(parts))
            lines.append("")

    # --- Key Metrics (compact, from JSON) ---
    metrics = findings.get("metrics", {})
    if metrics:
        red_metrics = []
        yellow_metrics = []
        green_metrics = []
        for key, name, bench, _ in _KEY_METRICS:
            val = metrics.get(key)
            if val is None:
                continue
            signal = _metric_signal(key, val)
            if isinstance(val, float):
                display = f"{val:.0f}%" if key != "large_pr_review_depth" else f"{val:.1f}"
            else:
                display = str(val)
            entry = f"{name}: {display} ({bench})"
            if ":red_circle:" in signal:
                red_metrics.append(f":red_circle: {entry}")
            elif ":large_yellow_circle:" in signal:
                yellow_metrics.append(f":large_yellow_circle: {entry}")
            else:
                green_metrics.append(f":large_green_circle: {entry}")

        if red_metrics or yellow_metrics or green_metrics:
            lines.append("*Key Metrics*")
            for m in red_metrics + yellow_metrics + green_metrics:
                lines.append(f"• {m}")

            # Add a few extra non-percentage metrics inline
            pr_states = []
            for k, label in [("pr_state_merged", "merged"), ("pr_state_open", "open"), ("pr_state_declined", "declined")]:
                v = metrics.get(k, 0)
                if v:
                    pr_states.append(f"{v} {label}")
            avg_loc = metrics.get("avg_pr_size_loc", 0)
            if pr_states or avg_loc:
                extra = []
                if pr_states:
                    extra.append("States: " + ", ".join(pr_states))
                if avg_loc:
                    extra.append(f"Avg LOC: {avg_loc}")
                lines.append(f"• {' · '.join(extra)}")
            lines.append("")

    # --- Recommended Skill Updates (from markdown — just the action lines) ---
    skill_match = re.search(
        r"(?:^|\n)#{1,3}\s+Recommended\s+Skill\s+Updates?\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
        report_md, re.DOTALL | re.IGNORECASE,
    )
    if skill_match:
        skill_text = skill_match.group(1).strip()
        skill_bullets = []
        for line in skill_text.splitlines():
            line = line.strip()
            if line.startswith(("• Action:", "- Action:", "* Action:")):
                skill_bullets.append("• " + line.split("|", 1)[0].strip().lstrip("•-* "))
            elif re.match(r"^[•\-\*]\s*(Update|Create)\b", line):
                skill_bullets.append("• " + line.lstrip("•-* "))
        if skill_bullets:
            lines.append("*Recommended Skill Updates*")
            lines.extend(skill_bullets[:5])
            lines.append("")

    # --- Positive Patterns (just names, no full text) ---
    pos_match = re.search(
        r"(?:^|\n)#{1,3}\s+Positive\s+Patterns?\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
        report_md, re.DOTALL | re.IGNORECASE,
    )
    if pos_match:
        pos_text = pos_match.group(1).strip()
        pos_names = []
        for line in pos_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            hm = re.match(r"^#{1,4}\s+(.+)", stripped)
            if hm:
                pos_names.append(hm.group(1).strip())
            elif "—" in stripped and not stripped.startswith(("•", "-", "*", "|", ">")):
                name = stripped.split("—")[0].strip().strip("*")
                if name and len(name) < 80:
                    pos_names.append(name)
        if pos_names:
            lines.append("*Positive Patterns*")
            for name in pos_names[:4]:
                lines.append(f"• {name}")
            lines.append("")

    return "\n".join(lines)


def _append_trimmed_sections(lines: list[str], report_md: str) -> None:
    """Fallback: extract finding sections from markdown when JSON findings are empty."""
    for section_name in ("Shipped Gaps", "In-Flight Concerns"):
        match = re.search(
            r"(?:^|\n)#{1,3}\s+" + re.escape(section_name) + r".*?\n(.*?)(?=\n#{1,3}\s|\Z)",
            report_md, re.DOTALL | re.IGNORECASE,
        )
        if match:
            section = match.group(1).strip()
            if len(section) > 1500:
                section = section[:1500].rsplit("\n", 1)[0] + "\n..."
            lines.append(f"*{section_name}*")
            lines.append(section)
            lines.append("")


def _sev_rank(severity: str) -> int:
    """Sort key: CRITICAL=0, HIGH=1, MEDIUM=2, LOW=3."""
    return {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}.get(severity.upper(), 4)


def _strip_verbose_sections(text: str) -> str:
    """Remove verbose sections from markdown before Slack formatting.

    Used as a secondary trim when the full report is posted (--full flag)
    but we still want to drop per-PR tables.
    """
    for section_title in _DROP_SECTIONS:
        pattern = re.compile(
            r"(^|\n)(#{1,3}\s+" + re.escape(section_title) + r"[^\n]*\n.*?)(?=\n#{1,3}\s|\Z)",
            re.DOTALL | re.IGNORECASE,
        )
        text = pattern.sub("", text)
    return text


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    reports_dir = workspace_dir / "reports"

    full_path = reports_dir / "pr_audit_report.md"
    if not full_path.exists():
        full_path = workspace_dir / "pr_audit_report.md"
    if not full_path.exists():
        print("ERROR: pr_audit_report.md not found -- run pr-audit step first", file=sys.stderr)
        sys.exit(1)

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
    findings_data: dict = {}

    findings_path = reports_dir / "pr_audit_findings.json"
    if findings_path.exists():
        try:
            findings_data = json.loads(findings_path.read_text(encoding="utf-8"))
            spec_gaps = findings_data.get("spec_gap_count", 0)
            design_gaps = findings_data.get("design_gap_count", 0)
            skill_gaps = findings_data.get("skill_gap_count", 0)
            practice_gaps = findings_data.get("practice_gap_count", 0)
            repo = findings_data.get("repo", "")
            branch = findings_data.get("branch", "")
            prs_analyzed = findings_data.get("prs_analyzed", "?")
            date_from = findings_data.get("date_from", "")
            date_to = findings_data.get("date_to", "")
            jiras_reviewed = findings_data.get("jiras_reviewed", 0)
        except Exception as e:
            print(f"[post-pr-audit] warning: could not parse findings JSON: {e}", flush=True)
    else:
        print("[post-pr-audit] pr_audit_findings.json not found -- counts will be 0", flush=True)

    # --- Build header ---
    reports_url, _ = build_artifact_links(config)
    artifact_link = f"\n:paperclip: <{reports_url}|View reports>" if reports_url else ""

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

    # --- Choose Slack body: digest (default) or full (--full flag) ---
    use_full = is_full_report(config)
    if use_full:
        report_text = _strip_verbose_sections(full_report_text)
        print("[post-pr-audit] posting full report (--full) with verbose sections trimmed", flush=True)
    else:
        digest = _build_slack_digest(findings_data, full_report_text)
        if digest.strip():
            report_text = digest
            print("[post-pr-audit] posting concise Slack digest", flush=True)
        else:
            report_text = _strip_verbose_sections(full_report_text)
            print("[post-pr-audit] digest empty — falling back to trimmed full report", flush=True)

    # Prepend consistent metadata header to HTML/MD artifact (full report, untouched)
    meta: list[str] = [f"**{prs_analyzed} PRs analyzed**"]
    if date_from and date_to:
        meta.append(f"{date_from} → {date_to}")
    elif date_from:
        meta.append(date_from)
    if jiras_reviewed:
        meta.append(f"{jiras_reviewed} Jira issues reviewed")
    summary = f"**{spec_gaps} spec | {design_gaps} design | {skill_gaps} skill | {practice_gaps} practice gaps**"
    md_header = build_md_report_header("PR Audit", repo or "repo", branch, meta, summary)
    body = re.sub(r"^#{1,3}\s+PR Audit[^\n]*\n", "", full_report_text.lstrip(), count=1)
    full_report_text_with_header = md_header + body

    # --- Format and post ---
    title = f"PR Audit — {repo or 'repo'}" + (f" @ {branch}" if branch else "")
    slack_text = format_for_slack(header + report_text)
    thread_ts = config.get("SLACK_THREAD_TS") or None
    slack_ok = post_report(config, slack_text, full_report_text_with_header,
                           title=title, filename="pr_audit_report.html",
                           thread_ts=thread_ts, task_type="audit-prs")

    result = {
        "status": "OK" if slack_ok else "SLACK_FAILED",
        "spec_gap_count": spec_gaps,
        "design_gap_count": design_gaps,
        "skill_gap_count": skill_gaps,
        "practice_gap_count": practice_gaps,
        "report_bytes": len(full_report_text_with_header),
        "slack_bytes": len(report_text),
        "slack_posted": slack_ok,
        "digest_mode": "full" if use_full else "digest",
    }
    (reports_dir / "post_pr_audit_result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8",
    )
    print(
        f"[post-pr-audit] spec={spec_gaps} design={design_gaps} skill={skill_gaps} "
        f"practice={practice_gaps} slack={'ok' if slack_ok else 'FAILED'} "
        f"mode={'full' if use_full else 'digest'} chars={len(slack_text)}",
        flush=True,
    )
    if not slack_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
