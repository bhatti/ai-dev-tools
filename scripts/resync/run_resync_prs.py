# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resync open PRs with their base branch.

Slack: @bot resync-prs <url|#N|NNNNN...> [--dry-run] [--tracker github|jira]
       @bot resync-prs --me [--dry-run]

A target is REQUIRED — either explicit PR URL(s)/number(s) or --me.
Omitting both exits with code 1 (prevents accidental mass sync).

Safety:
  - --me mode: author guard — only PRs authored by current user are processed
  - Explicit PR URLs/numbers: no author guard (user intent is clear)
  - Diff snapshot before/after merge — must match within 5% threshold
  - --dry-run never pushes anything

Writes:
  /workspace/reports/resync_prs_report.md
  /workspace/reports/resync_prs_report.html
  /workspace/reports/resync_prs_summary.json
  /workspace/logs/resync_prs.log
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.analyze.pr_fetcher import fetch_prs_by_numbers, parse_pr_url
from scripts.common.config import get_workspace_dir, load_config
from scripts.common.report_renderer import render_simple_html
from scripts.resync.pr_syncer import SyncResult, sync_pr


# ---------------------------------------------------------------------------
# Flag parsing
# ---------------------------------------------------------------------------

def _parse_slack_flags(message: str) -> dict:
    """Parse flags from SLACK_MESSAGE.

    Extracts: --dry-run, --me, --tracker, PR URLs, bare PR numbers.

    Targeting rules:
      --me           → auto-discover all open PRs authored by current user
      <URL|#N|NNNNN> → explicit mode; only those PRs are processed
      (neither)      → error; caller must reject and ask user to be explicit
    """
    flags: dict = {
        "dry_run": False,
        "me": False,
        "tracker": "",
        "pr_urls": [],
        "pr_numbers": [],
    }

    if re.search(r"--dry[-_]?run\b", message, re.IGNORECASE):
        flags["dry_run"] = True

    if re.search(r"--me\b", message, re.IGNORECASE):
        flags["me"] = True

    m = re.search(r"--tracker\s+(\S+)", message, re.IGNORECASE)
    if m:
        flags["tracker"] = m.group(1).lower()

    # PR URLs from message
    pr_url_re = re.compile(
        r"https?://(?:github\.com|bitbucket\.org)/\S+/(?:pull|pull-requests)/(\d+)"
    )
    url_spans: list[tuple[int, int]] = []
    for m in pr_url_re.finditer(message):
        flags["pr_urls"].append(message[m.start():m.end()])
        url_spans.append((m.start(), m.end()))

    # Bare PR numbers — #N form (any size) or plain 5-6 digit numbers (>= 10000).
    # Small plain numbers are rejected (e.g. "@bot resync-prs fix 100 tests" must NOT
    # match 100), but large numbers like "48239 48240" are unambiguously PR refs.
    cleaned = pr_url_re.sub("", message)
    seen_numbers: set[int] = set()
    for m in re.finditer(r"#(\d+)\b", cleaned):
        n = int(m.group(1))
        if 1 <= n <= 999999 and n not in seen_numbers:
            seen_numbers.add(n)
            flags["pr_numbers"].append(n)
    # Accept plain 5-6 digit numbers >= 10000 (very unlikely to be accidental in PR commands)
    cleaned_no_hash = re.sub(r"#\d+", "", cleaned)
    for m in re.finditer(r"\b(\d{5,6})\b", cleaned_no_hash):
        n = int(m.group(1))
        if 10000 <= n <= 999999 and n not in seen_numbers:
            seen_numbers.add(n)
            flags["pr_numbers"].append(n)

    return flags


# ---------------------------------------------------------------------------
# Current user identity
# ---------------------------------------------------------------------------

def _get_current_user_github(config: dict) -> str:
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            login = result.stdout.strip()
            if login:
                return login
    except Exception as exc:
        print(f"[resync] warn: gh api user failed: {exc}", flush=True)
    return config.get("GH_USER", config.get("GITHUB_USER", ""))


def _get_current_user_bitbucket(config: dict) -> dict:
    """Returns dict with account_id, display_name, nickname."""
    try:
        import requests
        bb_token = config.get("BITBUCKET_TOKEN", config.get("BITBUCKET_APP_PASSWORD", ""))
        bb_user = config.get("BITBUCKET_USERNAME", config.get("BITBUCKET_USER", ""))
        if not bb_token:
            return {}
        auth = (bb_user, bb_token) if bb_user else None
        headers = {} if bb_user else {"Authorization": f"Bearer {bb_token}"}
        resp = requests.get(
            "https://api.bitbucket.org/2.0/user",
            auth=auth, headers=headers, timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        print(f"[resync] warn: BB /user failed: {exc}", flush=True)
    return {}


# ---------------------------------------------------------------------------
# PR fetching
# ---------------------------------------------------------------------------

def _fetch_my_open_gh_prs(config: dict) -> list[dict]:
    """Fetch open PRs authored by @me on GitHub."""
    org = config.get("GH_ORG", "")
    repo = config.get("GH_REPO", "")
    if not org or not repo:
        print("[resync] warn: GH_ORG or GH_REPO not set", flush=True)
        return []
    try:
        fields = (
            "number,title,state,url,headRefName,baseRefName,"
            "author,reviewDecision,statusCheckRollup,reviews"
        )
        result = subprocess.run(
            ["gh", "pr", "list", "-R", f"{org}/{repo}",
             "--author", "@me", "--state", "open",
             "--json", fields, "--limit", "50"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"[resync] gh pr list failed: {result.stderr.strip()[:200]}", flush=True)
            return []
        return [_normalize_gh_pr(p) for p in json.loads(result.stdout)]
    except Exception as exc:
        print(f"[resync] warn: fetch my GH PRs failed: {exc}", flush=True)
        return []


def _normalize_gh_pr(raw: dict) -> dict:
    author_obj = raw.get("author") or {}
    author = author_obj.get("login", "")

    # CI status from statusCheckRollup
    ci_status = "unknown"
    checks = raw.get("statusCheckRollup") or []
    if checks:
        states = [c.get("state", "").lower() for c in checks if c.get("state")]
        if all(s in ("success", "neutral") for s in states):
            ci_status = "pass"
        elif any(s in ("failure", "error", "timed_out") for s in states):
            ci_status = "fail"
        elif any(s in ("pending", "in_progress", "queued", "waiting") for s in states):
            ci_status = "pending"

    reviews = raw.get("reviews") or []
    approved = sum(1 for r in reviews if r.get("state") == "APPROVED")
    changes = sum(1 for r in reviews if r.get("state") == "CHANGES_REQUESTED")

    return {
        "number": raw.get("number"),
        "title": raw.get("title", ""),
        "url": raw.get("url", ""),
        "state": "open",
        "author": author,
        "headRefName": raw.get("headRefName", ""),
        "baseRefName": raw.get("baseRefName", "main"),
        "ci_status": ci_status,
        "approved_count": approved,
        "changes_requested_count": changes,
        "pending_count": 0,
        "review_decision": raw.get("reviewDecision", ""),
    }


def _fetch_my_open_bb_prs(config: dict, current_user_info: dict) -> list[dict]:
    """Fetch open Bitbucket PRs authored by the current user."""
    try:
        import requests
        workspace = config.get("BITBUCKET_WORKSPACE", "")
        repo = config.get("BITBUCKET_REPO", "")
        bb_token = config.get("BITBUCKET_TOKEN", config.get("BITBUCKET_APP_PASSWORD", ""))
        bb_user = config.get("BITBUCKET_USERNAME", config.get("BITBUCKET_USER", ""))
        account_id = current_user_info.get("account_id", "")
        display_name = current_user_info.get("display_name", "")
        nickname = current_user_info.get("nickname", "")

        if not workspace or not repo or not bb_token:
            print("[resync] warn: BB workspace/repo/token not set", flush=True)
            return []

        auth = (bb_user, bb_token) if bb_user else None
        headers = {} if bb_user else {"Authorization": f"Bearer {bb_token}"}
        url: str | None = (
            f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}/pullrequests"
        )
        params: dict = {"state": "OPEN", "pagelen": 50}
        my_prs: list[dict] = []

        while url:
            resp = requests.get(url, params=params, auth=auth, headers=headers, timeout=30)
            if resp.status_code != 200:
                print(f"[resync] BB API error {resp.status_code}: {resp.text[:200]}", flush=True)
                break
            data = resp.json()
            for rp in data.get("values", []):
                a = rp.get("author", {})
                if (
                    (account_id and a.get("account_id") == account_id)
                    or (display_name and a.get("display_name") == display_name)
                    or (nickname and a.get("nickname") == nickname)
                ):
                    my_prs.append(_normalize_bb_pr(rp))
            url = data.get("next")
            params = {}
        return my_prs
    except Exception as exc:
        print(f"[resync] warn: fetch my BB PRs failed: {exc}", flush=True)
        return []


def _normalize_bb_pr(raw: dict) -> dict:
    author_obj = raw.get("author", {})
    source = raw.get("source", {})
    dest = raw.get("destination", {})
    participants = raw.get("participants", [])
    reviewers = [p for p in participants if p.get("role") == "REVIEWER"]
    approved = sum(1 for p in reviewers if p.get("approved"))
    # Bitbucket doesn't have a formal "changes_requested" state in the participants
    # endpoint — a reviewer who hasn't approved counts as pending/needs-review.
    # Use unapproved reviewers as a conservative proxy for pending review.
    changes_requested = 0  # no reliable BB API field without /diffstat/tasks endpoint
    pending = sum(1 for p in reviewers if not p.get("approved"))
    return {
        "number": raw.get("id"),
        "title": raw.get("title", ""),
        "url": raw.get("links", {}).get("html", {}).get("href", ""),
        "state": "open",
        "author": author_obj.get("display_name", author_obj.get("nickname", "")),
        "headRefName": source.get("branch", {}).get("name", ""),
        "baseRefName": dest.get("branch", {}).get("name", "main"),
        "ci_status": "unknown",
        "approved_count": approved,
        "changes_requested_count": changes_requested,
        "pending_count": pending,
    }


def _fetch_target_prs(
    config: dict,
    tracker: str,
    current_user: str,
    current_user_bb: dict,
    pr_urls: list[str],
    pr_numbers: list[int],
    me: bool = False,
) -> tuple[list[dict], bool]:
    """Return (prs, explicit_mode).

    explicit_mode=True when pr_urls or pr_numbers were given — author guard is disabled.
    me=True triggers auto-discovery of the current user's open PRs (author guard active).
    Callers must pass either explicit PR refs OR me=True; passing neither is a caller error.
    """
    if pr_urls or pr_numbers:
        # Build deduplicated list preserving order (URL and bare number may overlap)
        seen: set[int] = set()
        numbers: list[int] = []
        for n in pr_numbers:
            if n not in seen:
                seen.add(n)
                numbers.append(n)
        for url in pr_urls:
            try:
                _, num = parse_pr_url(url)
                if num not in seen:
                    seen.add(num)
                    numbers.append(num)
            except ValueError as e:
                print(f"[resync] warn: cannot parse PR URL {url!r}: {e}", flush=True)
        prs = fetch_prs_by_numbers(config, numbers) if numbers else []
        return prs, True

    # --me: auto-discover all open PRs authored by the current user
    if me:
        if tracker in ("jira", "bitbucket", "jira/bitbucket"):
            return _fetch_my_open_bb_prs(config, current_user_bb), False
        return _fetch_my_open_gh_prs(config), False

    # No target specified — caller should have rejected before reaching here
    return [], False


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

_STATUS_EMOJI = {
    "synced": "✅",
    "up_to_date": "✅",
    "conflict": "⚠️",
    "skipped": "⏭️",
    "error": "❌",
}

_CI_LABEL = {"pass": "✅ Passing", "fail": "❌ Failing", "pending": "⏳ Pending"}


def _build_report_md(
    results: list[SyncResult],
    repo: str,
    dry_run: bool,
    as_of: str,
) -> str:
    if dry_run:
        mode_banner = (
            "> 🔍 **DRY-RUN MODE** — No changes were pushed. "
            "All merges were simulated locally and then discarded."
        )
    else:
        mode_banner = (
            "> 🚀 **LIVE SYNC** — Changes have been pushed to the remote branches listed below."
        )

    lines: list[str] = [
        f"# PR Resync Report — {repo}",
        f"*Generated: {as_of}*",
        "",
        mode_banner,
        "",
        "## Summary",
        "",
        "| # | Title | Branch | Status | Merged | Conflicts | Diff | CI | Reviewers |",
        "|---|-------|--------|--------|--------|-----------|------|----|-----------|",
    ]

    for r in results:
        emoji = _STATUS_EMOJI.get(r.status, "❓")
        branch_str = f"`{r.pr_branch} → {r.base_branch}`"
        status_str = f"{emoji} {r.status.replace('_', ' ').title()}"
        merged_str = f"+{r.commits_merged}" if r.commits_merged > 0 else "—"
        conflict_str = ", ".join(r.conflict_files[:3]) if r.conflict_files else "0"
        if r.diff_verified:
            diff_str = f"{r.before_diff_lines}→{r.after_diff_lines} ✅"
        elif r.status in ("conflict", "skipped", "up_to_date"):
            diff_str = "—"
        else:
            diff_str = "❌"
        ci_str = {"pass": "✅", "fail": "❌", "pending": "⏳", "unknown": "—"}.get(r.build_status, "—")
        rev = r.reviewers
        rev_str = f"{rev.get('approved', 0)} ✅" if rev.get("approved") else "0"
        pr_link = f"[#{r.pr_number}]({r.pr_url})" if r.pr_url else f"#{r.pr_number}"
        title_short = (r.pr_title or "")[:40]
        lines.append(
            f"| {pr_link} | {title_short} | {branch_str} | {status_str} "
            f"| {merged_str} | {conflict_str} | {diff_str} | {ci_str} | {rev_str} |"
        )

    lines += ["", "## Details", ""]

    for r in results:
        emoji = _STATUS_EMOJI.get(r.status, "❓")
        lines += [
            f"### {emoji} PR #{r.pr_number} — {r.pr_title}",
            f"- **URL**: {r.pr_url or '—'}",
            f"- **Branch**: `{r.pr_branch}` → `{r.base_branch}`",
            f"- **Status**: {r.status}",
        ]

        if r.status == "synced":
            lines.append(f"- **Commits merged from base**: {r.commits_merged}")
            lines.append(
                f"- **Diff integrity**: ✅ verified "
                f"({r.before_diff_lines} → {r.after_diff_lines} changed lines)"
            )
            if r.merge_commit:
                lines.append(f"- **Merge commit**: `{r.merge_commit}`")
            lines.append(f"- **Pushed**: {'No (dry-run)' if dry_run else '✅ Yes'}")

        elif r.status == "up_to_date":
            lines.append("- Already in sync with base branch — no merge needed")

        elif r.status == "conflict":
            lines.append(f"- **Conflicted files** ({len(r.conflict_files)}):")
            for f in r.conflict_files:
                lines.append(f"  - `{f}`")
            lines.append("- **Action required**: Manual conflict resolution needed")
            lines.append(
                "- **Tip**: `git fetch origin && git checkout "
                f"{r.pr_branch} && git merge origin/{r.base_branch}`"
            )

        elif r.status in ("skipped", "error"):
            lines.append(f"- **Reason**: {r.error or 'unknown'}")

        rev = r.reviewers
        if any(rev.values()):
            lines.append(
                f"- **Reviewers**: {rev.get('approved', 0)} approved, "
                f"{rev.get('changes_requested', 0)} changes requested, "
                f"{rev.get('pending', 0)} pending"
            )
        lines.append(f"- **CI Status**: {_CI_LABEL.get(r.build_status, 'Unknown')}")

        ready = (
            r.status in ("synced", "up_to_date")
            and r.build_status in ("pass", "unknown")
            and rev.get("approved", 0) > 0
            and rev.get("changes_requested", 0) == 0
        )
        lines.append(f"- **Ready to merge**: {'✅ Yes' if ready else '⚠️ No'}")
        lines.append("")

    synced = sum(1 for r in results if r.status == "synced")
    up_to_date = sum(1 for r in results if r.status == "up_to_date")
    conflicts = sum(1 for r in results if r.status == "conflict")
    errors = sum(1 for r in results if r.status == "error")
    skipped = sum(1 for r in results if r.status == "skipped")

    mode_footer = (
        "**Mode: DRY-RUN — no changes were pushed to any branch.**"
        if dry_run else
        "**Mode: LIVE SYNC — changes were pushed to remote.**"
    )
    lines += [
        "## Summary Counts",
        f"- Total PRs: {len(results)}",
        f"- ✅ Synced: {synced}",
        f"- ✅ Up to date: {up_to_date}",
        f"- ⚠️ Conflicts: {conflicts}",
        f"- ❌ Errors: {errors}",
        f"- ⏭️ Skipped: {skipped}",
        "",
        mode_footer,
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    reports_dir = workspace_dir / "reports"
    logs_dir = workspace_dir / "logs"
    reports_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    slack_message = config.get("SLACK_MESSAGE", "")
    flags = _parse_slack_flags(slack_message)

    dry_run = flags["dry_run"] or config.get("DRY_RUN", "").lower() in ("1", "true", "yes")

    # Tracker resolution: --tracker flag > env DEFAULT_TRACKER
    # Write resolved tracker back to config so all downstream code (fetch_prs_by_numbers
    # etc.) sees the same value — mirrors CLAUDE.md Tracker Resolution Rule.
    tracker = flags["tracker"] or (config.get("DEFAULT_TRACKER") or "github").lower()
    if tracker in ("jira", "jira/bitbucket"):
        tracker = "jira/bitbucket"
    config["DEFAULT_TRACKER"] = tracker

    # Additional PR URLs from env (set by formicary from trailing Slack args)
    env_pr_urls = config.get("RESYNC_PR_URLS", "")
    if env_pr_urls:
        for url in env_pr_urls.split(","):
            url = url.strip()
            if url:
                flags["pr_urls"].append(url)

    # --- Require explicit target to prevent accidental mass sync ---
    # Either provide PR URL(s)/number(s), or --me to opt into syncing all your open PRs.
    has_explicit = bool(flags["pr_urls"] or flags["pr_numbers"])
    if not has_explicit and not flags["me"]:
        msg = (
            "[resync] ERROR: no PR targets specified.\n"
            "  Provide one of:\n"
            "    • PR URL(s): @bot resync-prs https://github.com/org/repo/pull/42\n"
            "    • PR number(s): @bot resync-prs #42 #101  or  @bot resync-prs 48239 48240\n"
            "    • All your open PRs: @bot resync-prs --me [--dry-run]\n"
        )
        print(msg, file=sys.stderr, flush=True)
        repo_lbl = config.get("GH_REPO") or config.get("BITBUCKET_REPO") or "repo"
        error_md = (
            "# PR Resync — No Target Specified\n\n"
            "> ❌ **Error**: No PR targets provided. Provide PR URLs/numbers or `--me` "
            "to sync all your open PRs.\n\n"
            "**Examples:**\n"
            "- `@bot resync-prs https://github.com/org/repo/pull/42`\n"
            "- `@bot resync-prs #42 #101 --dry-run`\n"
            "- `@bot resync-prs 48239 48240 --dry-run`\n"
            "- `@bot resync-prs --me --dry-run`\n"
        )
        (reports_dir / "resync_prs_report.md").write_text(error_md, encoding="utf-8")
        (reports_dir / "resync_prs_report.html").write_text(
            render_simple_html("PR Resync — Error", error_md), encoding="utf-8"
        )
        summary = {
            "total": 0, "synced": 0, "up_to_date": 0, "conflicts": 0,
            "errors": 1, "skipped": 0, "dry_run": dry_run,
            "tracker": tracker, "repo": repo_lbl, "as_of": "",
            "results": [],
            "error": "no PR targets specified — provide URL/number or --me",
        }
        (reports_dir / "resync_prs_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        sys.exit(1)

    # Print clear mode banner before any git work begins
    if dry_run:
        print("[resync] MODE: DRY-RUN — no changes will be pushed", flush=True)
    else:
        print("[resync] MODE: LIVE SYNC — changes WILL be pushed to remote branches", flush=True)

    print(
        f"[resync] tracker={tracker} dry_run={dry_run} me={flags['me']} "
        f"pr_urls={flags['pr_urls']} pr_numbers={flags['pr_numbers']}",
        flush=True,
    )

    # Current user identity
    current_user = ""
    current_user_bb: dict = {}
    if tracker in ("jira/bitbucket", "jira", "bitbucket"):
        current_user_bb = _get_current_user_bitbucket(config)
        current_user = current_user_bb.get("display_name", current_user_bb.get("nickname", ""))
    else:
        current_user = _get_current_user_github(config)

    print(f"[resync] current_user={current_user!r}", flush=True)

    # Fetch target PRs
    prs, explicit_mode = _fetch_target_prs(
        config, tracker, current_user, current_user_bb,
        flags["pr_urls"], flags["pr_numbers"], me=flags["me"],
    )
    print(f"[resync] {len(prs)} PR(s) to process (explicit_mode={explicit_mode})", flush=True)

    if not prs:
        print("[resync] no PRs found — nothing to do", flush=True)
        repo_lbl = config.get("GH_REPO") or config.get("BITBUCKET_REPO") or "repo"
        summary = {
            "total": 0, "synced": 0, "up_to_date": 0, "conflicts": 0,
            "errors": 0, "skipped": 0, "dry_run": dry_run,
            "tracker": tracker, "repo": repo_lbl, "as_of": "",
            "results": [],
        }
        (reports_dir / "resync_prs_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        # Write minimal report so post step and pod tests don't error on missing files
        empty_md = "# PR Resync Report\n\nNo open PRs found for the current user.\n"
        (reports_dir / "resync_prs_report.md").write_text(empty_md, encoding="utf-8")
        (reports_dir / "resync_prs_report.html").write_text(
            render_simple_html("PR Resync Report", empty_md), encoding="utf-8"
        )
        sys.exit(0)

    # Per-PR clone directory
    work_dir = workspace_dir / "clones"
    work_dir.mkdir(exist_ok=True)

    # Sync each PR sequentially (git operations are not thread-safe in a shared clone)
    results: list[SyncResult] = []
    for pr in prs:
        r = sync_pr(
            pr, config,
            dry_run=dry_run,
            current_user=current_user,
            tracker=tracker,
            author_guard=not explicit_mode,
            work_dir=work_dir,
        )
        results.append(r)

    # Build report artefacts
    org_label = config.get("GH_ORG") or config.get("BITBUCKET_WORKSPACE") or ""
    repo_label = config.get("GH_REPO") or config.get("BITBUCKET_REPO") or "repo"
    full_repo = f"{org_label}/{repo_label}" if org_label else repo_label
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    report_md = _build_report_md(results, full_repo, dry_run, as_of)
    report_html = render_simple_html(f"PR Resync — {full_repo}", report_md)

    (reports_dir / "resync_prs_report.md").write_text(report_md, encoding="utf-8")
    (reports_dir / "resync_prs_report.html").write_text(report_html, encoding="utf-8")

    synced = sum(1 for r in results if r.status == "synced")
    up_to_date = sum(1 for r in results if r.status == "up_to_date")
    conflicts = sum(1 for r in results if r.status == "conflict")
    errors = sum(1 for r in results if r.status == "error")
    skipped = sum(1 for r in results if r.status == "skipped")

    summary = {
        "total": len(results),
        "synced": synced,
        "up_to_date": up_to_date,
        "conflicts": conflicts,
        "errors": errors,
        "skipped": skipped,
        "dry_run": dry_run,
        "tracker": tracker,
        "repo": full_repo,
        "as_of": as_of,
        "results": [
            {
                "pr_number": r.pr_number,
                "pr_title": r.pr_title,
                "pr_url": r.pr_url,
                "base_branch": r.base_branch,
                "pr_branch": r.pr_branch,
                "status": r.status,
                "commits_merged": r.commits_merged,
                "conflict_files": r.conflict_files,
                "before_diff_lines": r.before_diff_lines,
                "after_diff_lines": r.after_diff_lines,
                "diff_verified": r.diff_verified,
                "dry_run": r.dry_run,
                "build_status": r.build_status,
                "reviewers": r.reviewers,
                "merge_commit": r.merge_commit,
                "error": r.error,
            }
            for r in results
        ],
    }
    (reports_dir / "resync_prs_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print(
        f"[resync] done: total={len(results)} synced={synced} "
        f"up_to_date={up_to_date} conflicts={conflicts} "
        f"errors={errors} skipped={skipped}",
        flush=True,
    )

    # Non-zero exit only when every single PR errored (conflicts and skips are acceptable)
    if errors == len(results) and errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
