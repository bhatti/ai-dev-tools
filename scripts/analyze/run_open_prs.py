"""Open PR Dashboard — fetch all open PRs and build a rich analytics report.

Usage:
    python -m scripts.analyze.run_open_prs

Slack invocation:
    @sb-slack open-prs [--repo <url>] [--target-branch <branch>]

Required env (one of):
    ANTHROPIC_API_KEY or CLAUDE_CODE_USE_BEDROCK=1

Repo resolution (priority order):
  1. --repo flag in SLACK_MESSAGE
  2. CODEBASE_REPO_URL env
  3. DEFAULT_TRACKER=jira/bitbucket → BITBUCKET_WORKSPACE + BITBUCKET_REPO
  4. DEFAULT_TRACKER=github → GH_ORG + GH_REPO

Writes:
  /workspace/reports/open_prs_report.md
  /workspace/reports/open_prs_report.html
  /workspace/logs/open_prs.log

Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.pr_classify import build_open_pr_dashboard
from scripts.common.pr_metadata import enrich_pr_full, normalize_pr
from scripts.common.report_renderer import render_simple_html


def _parse_slack_flags(message: str) -> dict:
    """Parse --repo and --target-branch from SLACK_MESSAGE."""
    flags: dict = {}
    m = re.search(r"--repo\s+(\S+)", message, re.IGNORECASE)
    if m:
        flags["repo"] = m.group(1)
    m = re.search(r"--target-branch\s+(\S+)", message, re.IGNORECASE)
    if m:
        flags["target_branch"] = m.group(1)
    m = re.search(r"--label\s+(\S+)", message, re.IGNORECASE)
    if m:
        flags["label"] = m.group(1)
    return flags


def _fetch_open_prs(config: dict, repo_url: str | None, target_branch: str | None, label: str | None) -> list[dict]:
    """Fetch open PRs using the MQ shared helper."""
    try:
        from scripts.mq._shared import fetch_open_prs, apply_repo_override
        if repo_url:
            apply_repo_override(config, repo_url)
        prs = fetch_open_prs(config, label=label or "", repo_override=repo_url or "", target_branch=target_branch or "")
        return prs or []
    except Exception as exc:
        print(f"[open-prs] warn: fetch_open_prs failed: {exc}", flush=True)
        return []


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    logs_dir = workspace_dir / "logs"
    reports_dir = workspace_dir / "reports"
    logs_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    slack_message = config.get("SLACK_MESSAGE", "")
    flags = _parse_slack_flags(slack_message)
    repo_url = flags.get("repo") or config.get("CODEBASE_REPO_URL") or ""
    target_branch = flags.get("target_branch") or ""
    label = flags.get("label") or ""

    tracker = (config.get("DEFAULT_TRACKER") or "github").lower()
    repo_label = repo_url.rstrip("/").split("/")[-1] if repo_url else (
        config.get("GH_REPO") or config.get("BB_REPO_SLUG") or "unknown"
    )
    org_label = (
        config.get("GH_ORG") or config.get("BB_WORKSPACE") or ""
    )
    full_repo_label = f"{org_label}/{repo_label}" if org_label else repo_label

    print(f"[open-prs] fetching open PRs for {full_repo_label} (tracker={tracker})", flush=True)

    raw_prs = _fetch_open_prs(config, repo_url or None, target_branch or None, label or None)
    print(f"[open-prs] fetched {len(raw_prs)} open PR(s)", flush=True)

    # Normalize and enrich each PR
    prs: list[dict] = []
    for raw in raw_prs:
        pr = normalize_pr(raw, tracker=tracker)
        try:
            pr = enrich_pr_full(pr, files=None, config=config)
        except Exception as exc:
            print(f"[open-prs] warn: enrichment failed for PR#{pr.get('number','?')}: {exc}", flush=True)
        prs.append(pr)

    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    branch_info = f" → `{target_branch}`" if target_branch else ""
    label_info = f" (label=`{label}`)" if label else ""

    # Build report
    lines: list[str] = [
        f"# Open PR Dashboard — {full_repo_label}",
        "",
        f"**As of:** {now_str}{branch_info}{label_info}  "
        f"**Total open:** {len(prs)}",
        "",
    ]

    if not prs:
        lines.append("_No open PRs found._\n")
    else:
        lines.append(build_open_pr_dashboard(prs))

    # Summary JSON for post script
    high_risk = sum(1 for p in prs if p.get("risk_tier") == "high")
    stale = sum(1 for p in prs if (p.get("age_days") or 0) > 7)
    summary = {
        "repo": full_repo_label,
        "tracker": tracker,
        "target_branch": target_branch,
        "total_open": len(prs),
        "high_risk": high_risk,
        "stale_count": stale,
        "as_of": now_str,
    }
    (reports_dir / "open_prs_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )

    md = "\n".join(lines)
    (reports_dir / "open_prs_report.md").write_text(md, encoding="utf-8")

    html = render_simple_html(f"Open PR Dashboard — {full_repo_label}", md)
    (reports_dir / "open_prs_report.html").write_text(html, encoding="utf-8")

    print(
        f"[open-prs] done: total={len(prs)} high_risk={high_risk} stale={stale}",
        flush=True,
    )
    print(md)


if __name__ == "__main__":
    main()
