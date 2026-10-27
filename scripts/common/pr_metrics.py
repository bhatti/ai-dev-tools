"""PR metrics: each metric is defined once and rendered by every PR report.

A :class:`Metric` knows how to show itself as a column of a per-PR table (``cell``) and as a row of the one
single-PR metrics table (``row``), which signal group it needs (``needs``, see ``pr_signals``), and its benchmark.
``build_pr_table`` / ``build_single_pr_metrics`` are the only table builders; merge queue, open PRs, PR queue, PR audit,
PR risk and PR review call them, so a new metric is one registry entry.

PR dicts may carry ``signals`` (a ``pr_signals`` record) and ``ai_findings`` (``severity.count_findings`` output).
A missing signal renders as ``—``; a failed fetch renders as ``n/a`` — never as zero.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable

from scripts.common import defect_origin
from scripts.common.pr_metadata import compute_pr_age, format_pr_status
from scripts.common.pr_signals import collect_signals, pr_number

PR_TYPE_EMOJI: dict[str, str] = {
    "bug": "🐛", "feature": "✨", "refactor": "♻️", "chore": "🔧",
    "security": "🔒", "test": "🧪", "docs": "📝", "unknown": "❓",
}
RISK_EMOJI: dict[str, str] = {"high": "🔴", "medium": "🟡", "low": "🟢"}
PRIORITY_EMOJI: dict[str, str] = {"blocker": "🚨", "critical": "🔴", "high": "🟠"}

GOOD, WARN, BAD, NONE = "🟢", "🟡", "🔴", "—"
Row = tuple[str, str, str, str]   # value, benchmark, signal, description


# --- small helpers ----------------------------------------------------------------------------------------------

def _esc(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _signals(pr: dict) -> dict:
    s = pr.get("signals")
    return s if isinstance(s, dict) else {}


def _state(pr: dict, group: str) -> str:
    """``"ok"``, ``"missing"`` (never fetched) or ``"error"`` for a signal group of this PR."""
    s = _signals(pr)
    if not s:
        return "missing"
    if s.get("error") or group in s.get("group_errors", {}):
        return "error"
    return "ok" if group in s.get("groups", []) else "missing"


def _plus(pr: dict, group: str) -> str:
    return "+" if group in _signals(pr).get("truncated", []) else ""


def age_days(pr: dict) -> int:
    """Days since the PR was opened; falls back to ``age_hours`` for lists that carry no ``created_at``."""
    age = compute_pr_age(pr)
    if age == 0:
        hours = pr.get("age_hours", 0)
        return int(hours / 24) if hours else 0
    return age


def age_label(pr: dict) -> str:
    hours = pr.get("age_hours")
    if hours is None:
        days = pr.get("age_days") if pr.get("age_days") is not None else age_days(pr)
        hours = days * 24
    if hours < 24:
        return f"{hours:.0f}h" if hours else "—"
    days = hours / 24
    if days >= 60:
        return f"⛔ {days:.0f}d"
    if days >= 30:
        return f"🔴 {days:.0f}d"
    if days >= 14:
        return f"🟡 {days:.0f}d"
    return f"{days:.0f}d"


# --- cells (per-PR table) ---------------------------------------------------------------------------------------

def _c_status(pr, ctx):
    status = format_pr_status(pr)
    return status.split(" ", 1)[1] if " " in status else status


TOOLTIP_MAX = 300


def tooltip(text: str) -> str:
    """Text for a markdown link title (``[x](url "title")``): one line, no quotes, bounded. The HTML renderer shows it as a hover tooltip."""
    flat = " ".join((text or "").replace('"', "'").split())
    flat = flat[:TOOLTIP_MAX - 1] + "…" if len(flat) > TOOLTIP_MAX else flat
    return flat.rstrip("\\").replace("|", "\\|")  # a pipe ends the table cell; a trailing backslash would escape the closing quote


def link_with_tip(label: str, url: str, tip: str) -> str:
    """A markdown link whose hover text is ``tip``; a bare label when there is no URL."""
    if not url:
        return label
    tip = tooltip(tip)
    return f'[{label}]({url} "{tip}")' if tip else f"[{label}]({url})"


def _c_pr(pr, ctx):
    url = pr.get("url", "")
    tail = url.rstrip("/").split("/")[-1] if url else ""
    num = pr.get("pr_number") or pr.get("number") or (tail if tail.isdigit() else None) or pr.get("id", "?")
    return link_with_tip(f"#{num}", url, pr.get("title") or "")


def _c_category(pr, ctx):
    cat = pr.get("category", "unknown")
    cell = f"{cat}*" if pr.get("category_confidence", "") not in ("file_path", "label", "") else cat
    if cat in ("security", "authn_authz"):
        cell = f"⚠️ {cell}"
    return f"🔥 {cell}" if pr.get("is_hotspot") else cell


def _c_type(pr, ctx):
    pt = pr.get("pr_type", "unknown")
    flag = "🚧" if pr.get("is_wip_pr") else ("📝" if pr.get("is_docs_pr") else "")
    return f"{PR_TYPE_EMOJI.get(pt, '❓')}{flag} {pt}"


def _c_origin(pr, ctx):
    return defect_origin.cell(pr)


def _c_blast(pr, ctx):
    blast = pr.get("blast_radius", "low")
    return f"{RISK_EMOJI.get(blast, '⚪')} {blast}"


def _c_risk(pr, ctx):
    tier = pr.get("risk_tier", pr.get("blast_radius", "low"))
    return f"{RISK_EMOJI.get(tier, '⚪')} {pr.get('risk_score', 0):.0f}"


def _c_loc(pr, ctx):
    loc = pr.get("total_loc", 0)
    return f"{loc:,}" if loc else "—"


def _c_files(pr, ctx):
    return str(pr.get("file_count")) if pr.get("file_count") else "—"


def _c_ci(pr, ctx):
    if ctx.get("ci_unavailable"):
        return "N/A"
    return {"pass": "✅", "fail": "❌", "pending": "⏳"}.get(pr.get("ci_status", "unknown"), "—")


def _c_age(pr, ctx):
    return age_label(pr)


def _c_reviewers(pr, ctx):
    s = _signals(pr)
    if _state(pr, "reviewers") == "ok":
        human, bot, approved = s.get("reviewers_human", 0), s.get("reviewers_bot", 0), len(s.get("approvers", []))
        if not (human or bot):
            return "—"
        parts = [f"{approved}/{human} ✅" if approved else f"{human} 👤"]
        if bot:
            parts.append(f"{bot} 🤖")
        return " · ".join(parts)
    if _state(pr, "reviewers") == "error":
        return "n/a"
    approvals, reviewers = pr.get("approval_count", 0), pr.get("reviewer_count", len(pr.get("reviewers") or []))
    if approvals > 0:
        return f"{approvals}/{reviewers} ✅"
    return f"{reviewers} assigned" if reviewers else "—"


def _c_comments(pr, ctx):
    state = _state(pr, "comments")
    if state != "ok":
        return "n/a" if state == "error" else "—"
    s = _signals(pr)
    return f"{s.get('comments_human', 0)} 👤 · {s.get('comments_bot', 0)} 🤖{_plus(pr, 'comments')}"


def _c_tasks(pr, ctx):
    state = _state(pr, "tasks")
    if state != "ok":
        return "n/a" if state == "error" else "—"
    s = _signals(pr)
    return f"{s.get('tasks_human', 0)} 👤 · {s.get('tasks_bot', 0)} 🤖 ({s.get('tasks_unresolved', 0)} open){_plus(pr, 'tasks')}"


def major_sources(pr: dict) -> dict[str, int] | None:
    """Major/critical issue counts by source (``ai``, ``bot``, ``human``), or None when nothing could be counted."""
    sources: dict[str, int] = {}
    ai = pr.get("ai_findings")
    if ai is not None:
        sources["ai"] = ai.get("major", 0)
    if _state(pr, "comments") == "ok":
        s = _signals(pr)
        sources["bot"], sources["human"] = s.get("major_comments_bot", 0), s.get("major_comments_human", 0)
    return sources or None


def _major_text(sources: dict[str, int]) -> str:
    detail = " · ".join(f"{name} {n}" for name, n in sources.items() if n)
    total = sum(sources.values())
    return f"{total} ({detail})" if detail else str(total)


def _c_major(pr, ctx):
    sources = major_sources(pr)
    return "—" if sources is None else _major_text(sources)


def _c_commits(pr, ctx):
    if _state(pr, "commits") != "ok":
        return "n/a" if _state(pr, "commits") == "error" else "—"
    return f"{_signals(pr).get('commits', 0)}{_plus(pr, 'commits')}"


def _c_author(pr, ctx):
    return "@" + ((pr.get("author") or "?").split() or ["?"])[0]


def _c_jira(pr, ctx):
    key, url = pr.get("jira_key", ""), pr.get("jira_url", "")
    return link_with_tip(key, url, pr.get("jira_summary") or pr.get("title") or "") if key and url else (key or "—")


def _c_priority(pr, ctx):
    priority = (pr.get("priority") or "").strip()
    return f"{PRIORITY_EMOJI.get(priority.lower(), '')} {priority}".strip() if priority else "—"


def _c_reviewer_names(pr, ctx):
    """Who approved, who is still pending (names), and the bots that reviewed (from comments or reviews)."""
    parts = []
    if pr.get("approved_by"):
        parts.append("✅ " + ", ".join("@" + n.split()[0] for n in pr["approved_by"][:3]))
    if pr.get("reviewers"):
        parts.append("🔔 " + ", ".join("@" + n.split()[0] for n in pr["reviewers"][:4]))
    if _state(pr, "reviewers") == "ok":
        bots = [r["name"] for r in _signals(pr).get("reviewers", []) if r["is_bot"]]
        if bots:
            parts.append("🤖 " + ", ".join(bots[:3]))
    return ";  ".join(parts) if parts else "—"


def _c_issues(pr, ctx):
    ref = pr.get("issue_ref") or pr.get("linked_issue") or {}
    key, url = (ref.get("key", ""), ref.get("url", "")) if isinstance(ref, dict) else (str(ref), "")
    return f"[{key}]({url})" if key and url else (key or "—")


# --- rows (single-PR table) -------------------------------------------------------------------------------------

def _grade(value: float, good: float, warn: float) -> str:
    return GOOD if value <= good else (WARN if value <= warn else BAD)


def _r_scope(pr):
    return (f"**{pr.get('scope', 'unknown')}**", "single owner ideal", NONE, pr.get("scope_description", "Ownership scope of the changed files"))


def _r_loc(pr):
    total = pr.get("total_loc", 0)
    return (f"{total} (+{pr.get('additions', 0)}/−{pr.get('deletions', 0)})", "≤300 healthy", _grade(total, 300, 800), "Total lines added + deleted")


def _r_files(pr):
    n = pr.get("file_count", 0)
    return (str(n), "≤5 healthy", _grade(n, 5, 15), "Number of files modified")


def _r_blast(pr):
    blast = pr.get("blast_radius", "low")
    return (blast, "low ideal", {"low": GOOD, "medium": WARN, "high": BAD}.get(blast, WARN), pr.get("blast_description", "Scope of potential impact"))


def _r_risk(pr):
    tier = pr.get("risk_tier", "—")
    if "risk_score" not in pr:
        return ("—", "<16 LOW", NONE, "Composite risk from 6 dimensions")
    return (f"{pr['risk_score']}/100 ({tier})", "<16 LOW", {"LOW": GOOD, "MEDIUM": WARN, "HIGH": BAD}.get(str(tier).upper(), WARN), "Composite risk from 6 dimensions")


def _r_categories(pr):
    cats = pr.get("categories") or []
    return (", ".join(cats[:4]), "—", NONE, "Domain areas touched") if cats else None


def _r_owners(pr):
    owners = pr.get("owners") or []
    return (", ".join(owners), "—", NONE, "CODEOWNERS entries responsible for review") if owners else None


def _r_sensitive(pr):
    paths = pr.get("sensitive_paths") or []
    return (", ".join(f"`{p}`" for p in paths[:5]), "none ideal", WARN, "Files matching auth/security/billing/infra patterns") if paths else None


def _r_origin(pr):
    origin = defect_origin.classify(pr)
    if origin is None:
        return None   # not a defect fix: no row
    signal = {"escaped": BAD if origin.confidence == "high" else WARN, "pre_release": GOOD}[origin.kind]
    return (defect_origin.cell(pr), "caught before release", signal, defect_origin.explain(pr))


def _r_age(pr):
    if not (pr.get("created_at") or pr.get("age_hours") or pr.get("age_days")):
        return None
    days = age_days(pr)
    return (f"{days}d", "<3d fresh", GOOD if days < 3 else (WARN if days < 14 else BAD), "Days since the PR was opened")


def _r_commits(pr):
    if _state(pr, "commits") != "ok":
        return ("n/a" if _state(pr, "commits") == "error" else "—", "≤5 healthy", NONE, "Commits excluding base-branch merges (churn)")
    n = _signals(pr).get("commits", 0)
    return (f"{n}{_plus(pr, 'commits')}", "≤5 healthy", _grade(n, 5, 10), "Commits excluding base-branch merges (churn)")


def _r_reviewers(pr):
    value = _c_reviewers(pr, {})
    human = _signals(pr).get("reviewers_human", 0) if _state(pr, "reviewers") == "ok" else None
    signal = NONE if human is None else (GOOD if human else BAD)
    return (value, "≥1 human reviewer", signal, "Assigned, reviewing or commenting; bots counted separately")


def _r_comments(pr):
    value = _c_comments(pr, {})
    if _state(pr, "comments") != "ok":
        return (value, "≥1 human comment", NONE, "Review comments by humans vs bots (inline + top-level)")
    s = _signals(pr)
    signal = GOOD if s.get("comments_human", 0) else (WARN if s.get("comments_bot", 0) else BAD)
    return (value, "≥1 human comment", signal, "Review comments by humans vs bots (inline + top-level)")


def _r_tasks(pr):
    value = _c_tasks(pr, {})
    if _state(pr, "tasks") != "ok":
        return (value, "0 open", NONE, "Review tasks/threads created, by humans vs bots")
    return (value, "0 open", GOOD if _signals(pr).get("tasks_unresolved", 0) == 0 else WARN, "Review tasks/threads created, by humans vs bots")


def _r_major(pr):
    sources = major_sources(pr)
    if sources is None:
        return ("—", "0", NONE, "CRITICAL/HIGH issues from AI review and from human/bot comments")
    total = sum(sources.values())
    return (_major_text(sources), "0", GOOD if total == 0 else BAD, "CRITICAL/HIGH issues from AI review and from human/bot comments")


NOT_ASSESSED = "Not assessed in this run: no value was reported"


def _sloppiness(pr: dict, key: str):
    """A value the review assessed for the change (``sloppiness`` in findings.json), or None: unassessed is not zero."""
    s = pr.get("sloppiness")
    value = s.get(key) if isinstance(s, dict) else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _r_verbosity(pr):
    pct = _sloppiness(pr, "verbosity_pct")
    if pct is None:
        return ("—", "<20% healthy", NONE, NOT_ASSESSED)
    return (f"{pct:g}%", "<20% healthy", GOOD if pct < 20 else (WARN if pct <= 30 else BAD),
            "Added lines that are trivial delegators, wrapper-of-wrappers, duplicated guards, redundant casts or dead code (shared/sloppiness-metrics.md)")


def _r_high_cc(pr):
    n = _sloppiness(pr, "high_cc_functions")
    if n is None:
        return ("—", "0 ideal", NONE, NOT_ASSESSED)
    return (str(int(n)), "0 ideal", GOOD if n == 0 else (WARN if n <= 3 else BAD), "Added or changed functions with cyclomatic complexity above 10")


@dataclass(frozen=True)
class Metric:
    id: str
    header: str
    cell: Callable[[dict, dict], str]
    label: str = ""
    row: Callable[[dict], Row | None] | None = None
    needs: str = ""     # pr_signals group this metric reads ("" = none)


REGISTRY: dict[str, Metric] = {m.id: m for m in (
    Metric("status", "Status", _c_status),
    Metric("pr", "PR", _c_pr),
    Metric("category", "Category", _c_category, "File Categories", _r_categories),
    Metric("type", "Type", _c_type),
    Metric("scope", "Scope", lambda pr, ctx: pr.get("scope", "—"), "Scope", _r_scope),
    Metric("origin", "Defect origin", _c_origin, "Defect Origin", _r_origin),
    Metric("blast", "Blast", _c_blast, "Blast Radius", _r_blast),
    Metric("risk", "Risk", _c_risk, "Risk Score", _r_risk),
    Metric("loc", "LOC", _c_loc, "LOC Changed", _r_loc),
    Metric("files", "Files", _c_files, "Files Changed", _r_files),
    Metric("owners", "Owners", lambda pr, ctx: ", ".join(pr.get("owners") or []) or "—", "Owners", _r_owners),
    Metric("sensitive", "Sensitive", lambda pr, ctx: ", ".join(pr.get("sensitive_paths") or []) or "—", "Sensitive Paths", _r_sensitive),
    Metric("target", "Target", lambda pr, ctx: pr.get("target_branch") or "—"),
    Metric("complexity", "Cx", lambda pr, ctx: RISK_EMOJI.get(pr.get("complexity", "low"), "—")),
    Metric("ci", "CI", _c_ci),
    Metric("age", "Age", _c_age, "PR Age", _r_age),
    Metric("commits", "Commits", _c_commits, "Commits", _r_commits, needs="commits"),
    Metric("reviewers", "Reviewers", _c_reviewers, "Reviewers", _r_reviewers, needs="reviewers"),
    Metric("comments", "Comments", _c_comments, "Comments (human · bot)", _r_comments, needs="comments"),
    Metric("tasks", "Tasks", _c_tasks, "Tasks (human · bot)", _r_tasks, needs="tasks"),
    Metric("major", "Major/Critical", _c_major, "Major/Critical Issues", _r_major, needs="comments"),
    Metric("issues", "Issues", _c_issues),
    Metric("author", "Author", _c_author),
    Metric("jira", "Jira", _c_jira),
    Metric("priority", "Priority", _c_priority),
    Metric("reviewer_names", "Reviewers", _c_reviewer_names, needs="reviewers"),
    Metric("verbosity", "Verbosity", lambda pr, ctx: _r_verbosity(pr)[0], "Verbosity (added lines)", _r_verbosity),
    Metric("high_cc", "High-CC", lambda pr, ctx: _r_high_cc(pr)[0], "High-CC functions (CC>10)", _r_high_cc),
)}

#: Columns of the per-PR table shown by every multi-PR report. There is no title column: the title is the hover text of the PR link
#: (and the Jira summary that of the Jira link), so rows stay short.
TABLE_COLUMNS = ("status", "pr", "category", "type", "origin", "blast", "risk", "loc", "files", "ci", "age",
                 "reviewers", "comments", "tasks", "major", "commits", "issues")
#: Stacked PRs (target a feature branch) show where they point and their complexity, and drop files.
STACKED_COLUMNS = ("pr", "target", "category", "type", "origin", "blast", "risk", "loc", "complexity", "ci", "age",
                   "reviewers", "comments", "tasks", "major", "commits", "issues")
#: The sprint PR queue (`prs`): who/what/when plus the same metrics, grouped by status by the caller.
QUEUE_COLUMNS = ("ci", "pr", "jira", "author", "age", "priority", "category", "type", "origin", "blast", "risk", "loc", "files",
                 "complexity", "reviewer_names", "comments", "tasks", "major", "commits")
#: Rows of the one single-PR metrics table, in display order.
SINGLE_ROWS = ("scope", "blast", "loc", "files", "risk", "category", "owners", "sensitive", "age", "origin", "commits", "reviewers",
               "comments", "tasks", "major", "verbosity", "high_cc")


#: Standup PR lines mention reviewers, comments, open tasks and major issues; the model writes the prose.
STANDUP_COLUMNS = ("reviewers", "comments", "tasks", "major")
#: Rows of the PR review report: it knows the findings and the review activity, not the scope files.
REVIEW_ROWS = ("commits", "reviewers", "comments", "tasks", "major", "verbosity", "high_cc")


def needs_for(metric_ids: tuple[str, ...] | list[str]) -> set[str]:
    """Signal groups to fetch for these metrics (the registry drives the fetch, so unshown metrics cost nothing)."""
    return {REGISTRY[i].needs for i in metric_ids if REGISTRY[i].needs}


def attach_signals(prs: list[dict], signals: dict[int, dict], findings: dict[int, dict] | None = None) -> None:
    """Put each PR's ``pr_signals`` record (and optional ``severity.count_findings`` result) on the PR dict."""
    for pr in prs:
        n = pr_number(pr)
        if n is None:
            continue
        if n in signals:
            pr["signals"] = signals[n]
        if findings and n in findings:
            pr["ai_findings"] = findings[n]


def build_pr_table(prs: list[dict], columns: tuple[str, ...] | list[str] = TABLE_COLUMNS, *, index: bool = False,
                   ci_unavailable: bool = False) -> list[str]:
    """The per-PR metrics table (header, separator, one line per PR) as markdown lines."""
    ctx = {"ci_unavailable": ci_unavailable}
    metrics = [REGISTRY[c] for c in columns]
    headers = (["#"] if index else []) + [m.header for m in metrics]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for i, pr in enumerate(prs, 1):
        cells = ([str(i)] if index else []) + [m.cell(pr, ctx) for m in metrics]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def build_single_pr_metrics(pr: dict, rows: tuple[str, ...] | list[str] = SINGLE_ROWS) -> list[str]:
    """The one single-PR metrics table: Metric | Value | Benchmark | Signal | Description (markdown lines)."""
    lines = ["| Metric | Value | Benchmark | Signal | Description |", "|--------|-------|-----------|--------|-------------|"]
    for rid in rows:
        metric = REGISTRY[rid]
        row = metric.row(pr) if metric.row else None
        if row:
            value, benchmark, signal, description = row
            lines.append(f"| {metric.label} | {_esc(value)} | {benchmark} | {signal} | {description} |")
    return lines


def aggregate_rows(prs: list[dict]) -> list[tuple[str, str, str, str, str]]:
    """Dashboard rows (metric, value, benchmark, signal, description) for the new signals across ``prs``.

    Only PRs whose signals were fetched count; nothing is returned when none were.
    """
    def ok(group):
        return [p for p in prs if _state(p, group) == "ok"]

    rows: list[tuple[str, str, str, str, str]] = []
    with_comments = ok("comments")
    if with_comments:
        no_human = sum(1 for p in with_comments if _signals(p).get("comments_human", 0) == 0)
        pct = 100 * no_human // len(with_comments)
        rows.append(("PRs without a human comment", f"{no_human}/{len(with_comments)} ({pct}%)", "0% ideal",
                     GOOD if pct == 0 else (WARN if pct <= 25 else BAD), "No human reviewed the change in comments"))
        human = sum(_signals(p).get("comments_human", 0) for p in with_comments)
        bot = sum(_signals(p).get("comments_bot", 0) for p in with_comments)
        rows.append(("Review comments (human · bot)", f"{human} · {bot}", "—", NONE, "Total inline + top-level comments across PRs"))
        major = sum(sum((major_sources(p) or {}).values()) for p in with_comments)
        rows.append(("Major/critical issues flagged", str(major), "0", GOOD if major == 0 else BAD, "CRITICAL/HIGH from comments and AI review"))
    with_tasks = ok("tasks")
    if with_tasks:
        total = sum(_signals(p).get("tasks_human", 0) + _signals(p).get("tasks_bot", 0) for p in with_tasks)
        open_ = sum(_signals(p).get("tasks_unresolved", 0) for p in with_tasks)
        rows.append(("Review tasks (open/total)", f"{open_}/{total}", "0 open", GOOD if open_ == 0 else WARN, "Tasks and review threads still unresolved"))
    with_commits = ok("commits")
    if with_commits:
        avg = sum(_signals(p).get("commits", 0) for p in with_commits) / len(with_commits)
        rows.append(("Avg commits per PR", f"{avg:.1f}", "≤5 healthy", _grade(avg, 5, 10), "Churn, merge commits excluded"))
    return rows


def enrich_with_signals(prs: list[dict], tracker: str, config: dict, columns: tuple[str, ...] | list[str] = TABLE_COLUMNS,
                        workspace=None) -> None:
    """Fetch the review signals the given columns need and attach them to ``prs`` (best effort, never raises).

    A failure leaves the PRs without signals, which tables render as ``—``.
    """
    needs = needs_for(columns)
    if not prs or not needs:
        return
    try:
        attach_signals(prs, collect_signals(prs, tracker, config, tuple(needs), workspace=workspace))
    except Exception as e:  # noqa: BLE001 - review signals are an enrichment; reports must still render
        print(f"[pr-metrics] review signals unavailable: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
