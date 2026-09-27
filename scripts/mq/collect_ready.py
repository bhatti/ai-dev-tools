"""Collect open PRs for merge queue analysis — no label required.

Usage:
    python -m scripts.mq.collect_ready                         # single repo (GH_ORG/GH_REPO or BB_WORKSPACE/BB_REPO)
    python -m scripts.mq.collect_ready --repo bhatti/todo-sample  # explicit repo override
    python -m scripts.mq.collect_ready --label ready-to-merge    # optional label filter (GH only)

Works for GitHub and Bitbucket via existing standup fetchers (gather_gh / bb_helpers).
Tracker resolved via DEFAULT_TRACKER or repo URL domain — same logic as all mq scripts.

Reads:  open PR list via gh CLI or Bitbucket REST (no special label needed)
Writes: /workspace/ready_prs.json

Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import click

from scripts.common.config import get_workspace_dir, load_config
from scripts.mq._shared import (
    _cfg_for_repo,
    fetch_open_prs,
    repo_slug,
)

_BUG_KEYWORDS = re.compile(r'\b(fix|bug|hotfix|patch|defect|regression|crash)\b', re.IGNORECASE)
_FEAT_KEYWORDS = re.compile(r'\b(feat|feature|story|enhancement|implement|add)\b', re.IGNORECASE)

# Canonical category definitions — see shared/merge-queue-metrics.md#pr-categories
# Order matters: first match wins. authn_authz before security so OAuth/IAM paths
# (which contain "auth" as a substring) match the more specific rule first.
_CATEGORY_RULES: list[tuple[str, list[str], list[str], list[str]]] = [
    # (category, path_patterns, label_keywords, title_re_patterns)
    ("authn_authz", [r"authn", r"authz", r"oauth", r"iam", r"rbac", r"saml", r"sso",
                     r"(^|/)auth(/|$)", r"token", r"session"],
                    ["auth", "authz", "rbac"],
                    [r"\bauth[nz]?\b", r"\bpermission\b", r"\baccess.control\b"]),
    ("security",    [r"crypto", r"secret", r"credential", r"cert", r"tls", r"ssl"],
                    ["security", "crypto", "cve"],
                    [r"\bsecurity\b", r"\bcve\b", r"\bvuln"]),
    ("sre",         [r"terraform", r"infra", r"k8s", r"kubernetes", r"helm", r"deploy", r"ansible", r"packer"],
                    ["terraform", "infra", "sre", "ops"],
                    [r"\bterraform\b", r"\binfra\b", r"\bk8s\b", r"\bdeploy\b"]),
    ("data",        [r"migration", r"schema", r"database", r"db/", r"sql", r"redis", r"kafka", r"etl"],
                    ["migration", "database", "schema"],
                    [r"\bmigration\b", r"\bschema\b", r"\bdatabase\b"]),
    ("api",         [r"api/", r"route", r"handler", r"controller", r"endpoint", r"grpc", r"proto"],
                    ["api", "grpc"],
                    [r"\bapi\b", r"\bendpoint\b", r"\broute\b"]),
    ("ui",          [r"frontend", r"web/", r"ui/", r"component", r"\.tsx?", r"\.vue", r"\.svelte", r"\.css", r"\.scss"],
                    ["frontend", "ui", "ux"],
                    [r"\bui\b", r"\bfrontend\b", r"\bcomponent\b"]),
    ("config",      [r"config", r"\.ya?ml", r"\.toml", r"\.env", r"settings"],
                    ["config", "configuration"],
                    [r"\bconfig\b", r"\bsettings\b"]),
    ("backend",     [r"src/", r"pkg/", r"lib/", r"service", r"core/"],
                    [],
                    []),
]


def _classify_pr_category(pr: dict, files: list[dict] | None = None) -> tuple[str, str]:
    """Classify PR into a domain category.

    Returns (category, confidence) where confidence is:
      'file_path' — derived from actual changed file paths (diffstat); most reliable
      'label'     — derived from PR labels
      'title'     — derived from PR title/description text
      'unknown'   — no signal found

    File paths are authoritative. Labels and title are fallbacks used when diffstat is
    unavailable (e.g., API call failed). See shared/merge-queue-metrics.md#pr-categories.
    """
    if files:
        paths_str = " ".join(f.get("path", "") for f in files).lower()
        for category, path_patterns, _, _ in _CATEGORY_RULES:
            if any(re.search(p, paths_str) for p in path_patterns):
                return category, "file_path"

    labels = [la.get("name", la) if isinstance(la, dict) else str(la) for la in pr.get("labels", [])]
    label_str = " ".join(labels).lower()
    for category, _, label_keywords, _ in _CATEGORY_RULES:
        if any(k in label_str for k in label_keywords):
            return category, "label"

    text = f"{pr.get('title', '')} {pr.get('description', '')}".lower()
    for category, _, _, title_pats in _CATEGORY_RULES:
        if title_pats and any(re.search(p, text) for p in title_pats):
            return category, "title"

    return "unknown", "unknown"


def _classify_pr_type(pr: dict) -> str:
    """Classify PR as bug/feature/unknown using labels first, then title keywords."""
    labels = [l.get("name", l) if isinstance(l, dict) else str(l) for l in pr.get("labels", [])]
    label_str = " ".join(labels).lower()
    if any(k in label_str for k in ("bug", "fix", "hotfix", "defect")):
        return "bug"
    if any(k in label_str for k in ("feature", "feat", "story", "enhancement")):
        return "feature"
    title = pr.get("title", "")
    if _BUG_KEYWORDS.search(title):
        return "bug"
    if _FEAT_KEYWORDS.search(title):
        return "feature"
    return "unknown"


def _enrich_prs_with_diffstat(prs: list[dict], config: dict) -> None:
    """Enrich each PR's blast_radius, scope, and category in-place using per-PR diffstat.

    One HTTP call per PR (BB: /diffstat endpoint; GH: gh pr view --json files).
    Best-effort: failures leave fields at their label/title-derived defaults.
    File paths are the authoritative signal for both category and blast_radius.
    """
    from scripts.mq._shared import fetch_pr_files
    from scripts.mq.scope_router import _compute_scope
    total = len(prs)
    if total:
        print(f"[collect_ready] enriching {total} PRs with diffstat (one API call per PR)...", flush=True)
    for i, pr in enumerate(prs):
        if i > 0 and i % 25 == 0:
            print(f"[collect_ready] enriched {i}/{total} PRs...", flush=True)
        try:
            files = fetch_pr_files(config, str(pr["pr_number"]))
            if files:
                scope_name, blast_radius, _, _ = _compute_scope(files, {})
                pr["blast_radius"] = blast_radius
                if scope_name and scope_name not in ("unknown", "cross-scope"):
                    pr["scope"] = scope_name
                # File-path category is authoritative — overrides label/title classification
                cat, confidence = _classify_pr_category(pr, files=files)
                pr["category"] = cat
                pr["category_confidence"] = confidence
        except Exception as exc:
            print(f"[collect_ready] warn: diffstat failed PR#{pr['pr_number']}: {exc}", flush=True)


def _compute_age_hours(created_at: str) -> float:
    """Compute PR age in hours from ISO 8601 timestamp."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() / 3600
    except (ValueError, AttributeError):
        return 0.0


def _normalize_pr(pr: dict, default_repo: str) -> dict:
    """Normalize a PR dict from gather_gh or bb_helpers to the ready_prs schema.

    gather_gh returns: id (=number), title, author (login str), branch, created,
        age_hours, reviewers, has_approval, approval_count, ci_status, url, labels
    bb_helpers returns: id, title, author (display_name str), branch, created,
        age_hours, reviewers
    Neither returns files/additions/deletions — blast_radius defaults to 'low'.
    scope_router.py does authoritative per-PR file analysis downstream.
    """
    pr_number = pr.get("number") or pr.get("id") or 0

    # age_hours already computed by both standup fetchers; fall back to timestamp parse
    age_hours = pr.get("age_hours") or _compute_age_hours(
        pr.get("createdAt", "") or pr.get("created", "")
    )

    # author: gather_gh returns login string; bb_helpers returns display_name string
    author = pr.get("author", "")
    if isinstance(author, dict):
        author_login = author.get("login") or author.get("display_name") or "unknown"
    else:
        author_login = str(author) or "unknown"

    # ci_status: gather_gh computes it; bb_helpers doesn't (no BB CI status API used)
    ci_status = pr.get("ci_status", "none")

    # approval: gather_gh has has_approval + approval_count; bb_helpers has neither yet
    has_approval = pr.get("has_approval") or (pr.get("approval_count", 0) > 0)

    category, category_confidence = _classify_pr_category(pr)  # label/title fallback; enriched below
    return {
        "pr_number": pr_number,
        "repo": pr.get("repo", default_repo),
        "title": pr.get("title", ""),
        "scope": "unknown",          # _enrich_prs_with_diffstat sets authoritative scope per PR
        "blast_radius": "low",       # _enrich_prs_with_diffstat sets authoritative blast_radius per PR
        "category": category,        # _enrich_prs_with_diffstat upgrades to 'file_path' confidence
        "category_confidence": category_confidence,
        "pr_type": _classify_pr_type(pr),
        "author": author_login,
        "age_hours": round(float(age_hours), 1),
        "branch": pr.get("headRefName", "") or pr.get("branch", ""),
        "target_branch": pr.get("target_branch", "") or pr.get("baseRefName", ""),
        "ci_status": ci_status,
        "has_approval": bool(has_approval),
        "url": pr.get("url", ""),
        "labels": pr.get("labels", []),
    }


@click.command()
@click.option("--label", default="", help="Optional label filter (empty = all open PRs; GH only)")
@click.option("--repo", default="", help="Repo override: full URL or org/repo slug")
@click.option("--target-branch", default="", envvar="TARGET_BRANCH",
              help="Only collect PRs targeting this branch (e.g. stage, main, dev). "
                   "Dramatically reduces PR count for large repos.")
def main(label: str, repo: str, target_branch: str) -> None:
    config = load_config(required=[])

    # _cfg_for_repo sets GH_ORG/GH_REPO (or BB equivalents) on a copy; merge it back
    # so repo_slug() and the artifact output reflect the actual repo that was queried.
    if repo:
        config = _cfg_for_repo(config, repo)

    raw_prs = fetch_open_prs(config, label=label, target_branch=target_branch)
    slug = repo_slug(config)
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"[collect_ready] repo={slug} target_branch={target_branch!r} label={label!r} prs_found={len(raw_prs)}", flush=True)

    ready_prs = [_normalize_pr(pr, slug) for pr in raw_prs]
    ready_prs.sort(key=lambda p: p["age_hours"], reverse=True)

    # Enrich blast_radius and scope via per-PR diffstat calls (best-effort)
    _enrich_prs_with_diffstat(ready_prs, config)

    result = {
        "pr_count": len(ready_prs),
        "repo": slug,
        "target_branch_filter": target_branch,
        "prs": ready_prs,
    }
    out_path = workspace / "ready_prs.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"[collect_ready] wrote {out_path} ({len(ready_prs)} PRs)", flush=True)
    print(f"::add-task-context TOTAL_PRS::{len(ready_prs)}", flush=True)
    if target_branch:
        print(f"::add-task-context TARGET_BRANCH::{target_branch}", flush=True)


if __name__ == "__main__":
    main()
