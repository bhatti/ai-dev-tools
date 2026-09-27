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
import os
import re
from datetime import datetime, timezone

import click

from scripts.common.config import get_workspace_dir, load_config
from scripts.mq._shared import (
    SENSITIVE_PATHS,
    _cfg_for_repo,
    fetch_open_prs,
    is_test_file,
    repo_slug,
)

_BUG_KEYWORDS = re.compile(
    r'\b(fix|bug|hotfix|hot.?fix|patch|defect|regression|crash|revert|rollback|roll.?back|roll.?forward|workaround|broken)\b',
    re.IGNORECASE,
)
_FEAT_KEYWORDS = re.compile(r'\b(feat|feature|story|enhancement|implement|add)\b', re.IGNORECASE)
_REFACTOR_KEYWORDS = re.compile(
    r'\b(refactor|cleanup|clean.up|detangle|extract|reorganize|restructure|simplify|split|rename|move)\b',
    re.IGNORECASE,
)
_CHORE_KEYWORDS = re.compile(
    r'\b(chore|deps?|dependency|upgrade|bump|update|version|migrate)\b',
    re.IGNORECASE,
)
_SECURITY_KEYWORDS = re.compile(
    r'\b(vulnerability|cve|rce|ssrf|xss|injection|exploit|0-?day|zero.?day|security.?fix)\b',
    re.IGNORECASE,
)

# Compiled at module level — called once per PR (250+ times per run)
_JIRA_KEY_RE = re.compile(r'\b([A-Z][A-Z0-9]{1,9}-\d+)\b')  # e.g. PROJ-123, AB-1
_GH_CLOSES_RE = re.compile(r'(?:closes?|fixes?|resolves?)\s+#(\d+)', re.IGNORECASE)
_GH_PR_URL_RE = re.compile(r'/pull/\d+.*')

# ---------------------------------------------------------------------------
# PR flag detection — env-var configurable title/branch patterns.
# File-path patterns (test file extensions) are universal and always active.
# Set env vars to "" to disable title/branch-pattern detection entirely.
# ---------------------------------------------------------------------------

def _compile_opt(env_key: str, default: str) -> re.Pattern | None:
    """Compile regex from env var; return None when env var is empty string."""
    pattern = os.environ.get(env_key, default)
    return re.compile(pattern, re.IGNORECASE) if pattern else None


# Test/SDET — title and branch patterns (env-configurable)
_TEST_TITLE_RE: re.Pattern | None = _compile_opt(
    "TEST_PR_TITLE_PATTERNS", r"\[SDET\]|\bSDET\b|\[QA\]|\bQA\b"
)
_TEST_BRANCH_PREFIXES: tuple[str, ...] = tuple(
    p.strip()
    for p in os.environ.get("TEST_BRANCH_PREFIXES", "sdet/,test/,tests/,qa/,e2e/").split(",")
    if p.strip()
)

# WIP / Draft / PoC (env-configurable)
_WIP_TITLE_RE: re.Pattern | None = _compile_opt(
    "WIP_PR_TITLE_PATTERNS", r"\[WIP\]|\bWIP\b|^DRAFT[:/]|\[PoC\]|\[POC\]"
)

# Docs-only PRs (env-configurable title; file patterns are universal)
_DOCS_TITLE_RE: re.Pattern | None = _compile_opt(
    "DOCS_PR_TITLE_PATTERNS", r"^docs?\(|^docs?:|^\[docs?\]"
)

# File-path patterns — universal, not org-specific
_TEST_FILE_RE = re.compile(
    r"(/__tests__/|/[Tt]ests?/|\.test\.[jt]sx?$|\.spec\.[jt]sx?$"
    r"|_test\.go$|[Tt]est\.java$|(?:^|/)test_[^/]+\.py$|[^/]+_test\.py$)",
    re.IGNORECASE,
)
_DOCS_FILE_RE = re.compile(r"\.md$|\.rst$|\.txt$|/docs?/", re.IGNORECASE)
_ASSET_FILE_RE = re.compile(r"\.svg$|/icons?/|/assets?/", re.IGNORECASE)

# Fraction of files that must be test files to declare a PR "test-only" (configurable)
_TEST_FILE_THRESHOLD = float(os.environ.get("TEST_FILE_THRESHOLD", "0.8"))

# Blast order and data-driven cap table
_BLAST_ORDER = ["low", "medium", "high"]
_PR_FLAG_BLAST_CAPS: dict[str, str] = {
    "is_test_pr": "low",
    "is_docs_pr": "low",
    "is_wip_pr":  "medium",
}

# Security-sensitive title/description keywords — force sensitive_paths score to max.
# Env-var configurable: set SECURITY_TITLE_KEYWORDS="" to disable.
_SECURITY_TITLE_RE: re.Pattern | None = _compile_opt(
    "SECURITY_TITLE_KEYWORDS",
    r"\b(vulnerability|cve|rce|injection|ssrf|xss|exploit|0-day|zero-day)\b",
)

# Category blast modifiers: added to blast_radius dimension score.
# See shared/merge-queue-metrics.md#pr-categories.
_CATEGORY_BLAST_MODIFIERS: dict[str, int] = {
    "security": 2, "authn_authz": 2,
    "sre": 1, "data": 1,
}

# Risk dimension weights — see shared/merge-queue-metrics.md#risk-dimensions
_RISK_WEIGHTS: dict[str, float] = {
    "size": 1.5, "file_count": 1.0, "blast_radius": 2.0,
    "sensitive_paths": 2.5, "test_coverage": 1.5, "historical": 1.0,
}

# Risk tier thresholds (composite score boundaries)
_RISK_TIER_LOW_MAX = 15
_RISK_TIER_MEDIUM_MAX = 30


def _compute_risk_score(pr: dict, files: list[dict] | None = None) -> dict:
    """Compute multi-dimension risk score per merge-queue-metrics.md.

    Returns {"risk_score": float, "risk_tier": str, "risk_dimensions": {dim: int}}.
    Works without files (graceful degradation using PR metadata only).
    """
    dims: dict[str, int] = {}

    # --- size: total lines changed ---
    total_loc = 0
    if files:
        total_loc = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
    if total_loc <= 20:
        dims["size"] = 1
    elif total_loc <= 50:
        dims["size"] = 2
    elif total_loc <= 100:
        dims["size"] = 3
    elif total_loc <= 200:
        dims["size"] = 5
    elif total_loc <= 500:
        dims["size"] = 7
    elif total_loc <= 1000:
        dims["size"] = 9
    else:
        dims["size"] = 10

    # --- file_count ---
    n_files = len(files) if files else 0
    if n_files <= 3:
        dims["file_count"] = 1
    elif n_files <= 5:
        dims["file_count"] = 2
    elif n_files <= 10:
        dims["file_count"] = 3
    elif n_files <= 20:
        dims["file_count"] = 5
    elif n_files <= 50:
        dims["file_count"] = 8
    else:
        dims["file_count"] = 10

    # --- blast_radius: from pre-computed field + category modifier ---
    blast = pr.get("blast_radius", "low")
    blast_score = {"low": 2, "medium": 5, "high": 9}.get(blast, 2)
    category = pr.get("category", "unknown")
    blast_score = min(10, blast_score + _CATEGORY_BLAST_MODIFIERS.get(category, 0))
    dims["blast_radius"] = blast_score

    # --- sensitive_paths: count non-test files matching SENSITIVE_PATHS regex ---
    sensitive_count = 0
    if files:
        sensitive_count = sum(
            1 for f in files
            if SENSITIVE_PATHS.search(f.get("path", "")) and not is_test_file(f.get("path", ""))
        )
    # Title keyword booster: security-related titles force max score
    text = f"{pr.get('title', '')} {pr.get('description', '') or ''}"
    if _SECURITY_TITLE_RE and _SECURITY_TITLE_RE.search(text):
        sensitive_count = max(sensitive_count, 5)
    if sensitive_count == 0:
        dims["sensitive_paths"] = 0
    elif sensitive_count == 1:
        dims["sensitive_paths"] = 3
    elif sensitive_count == 2:
        dims["sensitive_paths"] = 5
    elif sensitive_count == 3:
        dims["sensitive_paths"] = 7
    elif sensitive_count == 4:
        dims["sensitive_paths"] = 8
    else:
        dims["sensitive_paths"] = 10

    # --- test_coverage: ratio of test files to total files ---
    if files and n_files > 0:
        test_count = sum(1 for f in files if is_test_file(f.get("path", "")))
        ratio = test_count / n_files
        if ratio >= 1.0:
            dims["test_coverage"] = 0
        elif ratio >= 0.5:
            dims["test_coverage"] = 2
        elif ratio >= 0.25:
            dims["test_coverage"] = 4
        elif ratio > 0:
            dims["test_coverage"] = 6
        else:
            dims["test_coverage"] = 8
    else:
        dims["test_coverage"] = 3  # neutral default when no file data

    # --- historical: default neutral (no defect history integration yet) ---
    dims["historical"] = 3

    # --- composite score ---
    score = sum(dims[k] * _RISK_WEIGHTS[k] for k in dims)

    if score <= _RISK_TIER_LOW_MAX:
        tier = "low"
    elif score <= _RISK_TIER_MEDIUM_MAX:
        tier = "medium"
    else:
        tier = "high"

    return {"risk_score": round(score, 1), "risk_tier": tier, "risk_dimensions": dims}


def _classify_pr_flags(pr: dict, files: list[dict] | None = None) -> dict[str, bool]:
    """Return boolean flags describing special PR types.

    Keys: is_test_pr, is_wip_pr, is_docs_pr.

    File-path signals (always active when files available):
    - is_test_pr: TEST_FILE_THRESHOLD fraction of changed files are test files
    - is_docs_pr: all changed files are docs/markdown
    - (no file signal for WIP — title only)

    Title/branch signals (active when env vars non-empty):
    - is_test_pr: TEST_PR_TITLE_PATTERNS match or branch starts with TEST_BRANCH_PREFIXES
    - is_wip_pr:  WIP_PR_TITLE_PATTERNS match
    - is_docs_pr: DOCS_PR_TITLE_PATTERNS match or branch starts with docs/
    """
    title = pr.get("title", "")
    branch = pr.get("branch", "") or pr.get("headRefName", "")
    flags: dict[str, bool] = {"is_test_pr": False, "is_wip_pr": False, "is_docs_pr": False}

    # --- file-path signals (authoritative when available) ---
    if files:
        total = len(files)
        test_count = sum(1 for f in files if _TEST_FILE_RE.search(f.get("path", "")))
        if total and test_count / total >= _TEST_FILE_THRESHOLD:
            flags["is_test_pr"] = True
        if total and all(_DOCS_FILE_RE.search(f.get("path", "")) for f in files):
            flags["is_docs_pr"] = True
        if total and all(_ASSET_FILE_RE.search(f.get("path", "")) for f in files):
            flags["is_docs_pr"] = True  # asset-only treated as docs-level blast

    # --- title/branch signals (fallback; env-var disabled when pattern is "") ---
    if not flags["is_test_pr"]:
        if _TEST_TITLE_RE and _TEST_TITLE_RE.search(title):
            flags["is_test_pr"] = True
        elif _TEST_BRANCH_PREFIXES and branch and branch.startswith(_TEST_BRANCH_PREFIXES):
            flags["is_test_pr"] = True

    if not flags["is_wip_pr"] and _WIP_TITLE_RE and _WIP_TITLE_RE.search(title):
        flags["is_wip_pr"] = True

    if not flags["is_docs_pr"]:
        if _DOCS_TITLE_RE and _DOCS_TITLE_RE.search(title):
            flags["is_docs_pr"] = True
        elif branch.startswith("docs/"):
            flags["is_docs_pr"] = True

    return flags


def _apply_blast_cap(blast_radius: str, flags: dict[str, bool]) -> str:
    """Apply the lowest blast_radius cap for any active flag."""
    cap_idx = _BLAST_ORDER.index(blast_radius)
    for flag, cap in _PR_FLAG_BLAST_CAPS.items():
        if flags.get(flag):
            cap_idx = min(cap_idx, _BLAST_ORDER.index(cap))
    return _BLAST_ORDER[cap_idx]


# Canonical category definitions — see shared/merge-queue-metrics.md#pr-categories
# Order matters: first match wins.
# "test" MUST be first so test paths (tests/auth/) don't match authn_authz/security.
# authn_authz before security so OAuth/IAM paths match the more specific rule first.
_CATEGORY_RULES: list[tuple[str, list[str], list[str], list[str]]] = [
    # (category, path_patterns, label_keywords, title_re_patterns)
    ("test",       [r"/__tests__/", r"/test/", r"/tests/", r"\.test\.[jt]sx?$",
                    r"\.spec\.[jt]sx?$", r"_test\.go$", r"Test\.java$",
                    r"test_.*\.py$", r".*_test\.py$"],
                   ["test", "sdet", "qa"],
                   [r"\[SDET\]", r"\bSDET\b", r"\btest suite\b"]),
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


def _extract_issue_ref(pr: dict) -> dict | None:
    """Extract Jira or GitHub issue reference from PR title/description.

    Uses pre-compiled regexes (module-level) for performance at 250+ PRs/run.
    Env vars: JIRA_BASE_URL (consistent with existing scripts/analyze/pr_fetcher.py).
    Returns {"key": "FOO-123", "url": "https://..."} or None.
    Jira wins over GitHub closing refs when both appear in the same PR text.
    """
    text = f"{pr.get('title', '')} {pr.get('description', '') or pr.get('body', '')}"
    m = _JIRA_KEY_RE.search(text)
    if m:
        key = m.group(1)
        base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        url = f"{base}/browse/{key}" if base else ""
        return {"key": key, "url": url}
    m = _GH_CLOSES_RE.search(text)
    if m:
        num = m.group(1)
        pr_url = pr.get("url", "")
        repo_base = _GH_PR_URL_RE.sub("", pr_url)
        url = f"{repo_base}/issues/{num}" if repo_base else ""
        return {"key": f"#{num}", "url": url}
    return None


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


def _classify_pr_type(pr: dict, flags: dict[str, bool] | None = None) -> str:
    """Classify PR into work type: bug, feature, refactor, chore, security, test, docs, unknown.

    Priority: security (title keywords) > labels > title keywords > flags > unknown.
    Security-keyword PRs are always 'security' regardless of labels — a vulnerability
    fix labelled 'bug' is still a security fix for risk scoring purposes.
    """
    title = pr.get("title", "")
    description = pr.get("description", "") or pr.get("body", "") or ""
    text = f"{title} {description}"

    if _SECURITY_KEYWORDS.search(text):
        return "security"

    labels = [la.get("name", la) if isinstance(la, dict) else str(la) for la in pr.get("labels", [])]
    label_str = " ".join(labels).lower()

    if any(k in label_str for k in ("bug", "fix", "hotfix", "defect")):
        return "bug"
    if any(k in label_str for k in ("feature", "feat", "story", "enhancement")):
        return "feature"
    if any(k in label_str for k in ("refactor", "cleanup", "tech-debt", "tech_debt")):
        return "refactor"
    if any(k in label_str for k in ("chore", "deps", "dependency", "maintenance")):
        return "chore"

    if _BUG_KEYWORDS.search(title):
        return "bug"
    if _FEAT_KEYWORDS.search(title):
        return "feature"
    if _REFACTOR_KEYWORDS.search(title):
        return "refactor"
    if _CHORE_KEYWORDS.search(title):
        return "chore"

    if flags:
        if flags.get("is_test_pr"):
            return "test"
        if flags.get("is_docs_pr"):
            return "docs"

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
                if scope_name and scope_name not in ("unknown", "cross-scope"):
                    pr["scope"] = scope_name
                # Re-evaluate flags with file-path evidence (authoritative)
                flags = _classify_pr_flags(pr, files=files)
                pr["is_test_pr"] = flags["is_test_pr"]
                pr["is_wip_pr"] = flags["is_wip_pr"]
                pr["is_docs_pr"] = flags["is_docs_pr"]
                pr["pr_type"] = _classify_pr_type(pr, flags)
                # Category: test category wins — tests/auth/ must not become authn_authz
                if flags["is_test_pr"]:
                    pr["category"] = "test"
                    pr["category_confidence"] = "file_path"
                else:
                    cat, confidence = _classify_pr_category(pr, files=files)
                    pr["category"] = cat
                    pr["category_confidence"] = confidence
                # Apply blast cap AFTER category is set (data-driven cap table)
                pr["blast_radius"] = _apply_blast_cap(blast_radius, flags)
                # Compute multi-dimension risk score (uses blast_radius + category already set)
                risk = _compute_risk_score(pr, files)
                pr["risk_score"] = risk["risk_score"]
                pr["risk_tier"] = risk["risk_tier"]
                pr["risk_dimensions"] = risk["risk_dimensions"]
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
    n = pr.get("number")
    pr_number = n if n is not None else pr.get("id", 0)

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

    # approval: gather_gh has has_approval + approval_count; bb_helpers extracts from participants
    approval_count = pr.get("approval_count", 0)
    has_approval = pr.get("has_approval") or (approval_count > 0)
    reviewer_count = pr.get("reviewer_count") or len(pr.get("reviewers", []))

    # Title-only pass for flags (no files yet); _enrich_prs_with_diffstat refines with file paths
    flags = _classify_pr_flags(pr)
    category, category_confidence = _classify_pr_category(pr)
    if flags["is_test_pr"]:
        category, category_confidence = "test", "title"
    return {
        "pr_number": pr_number,
        "repo": pr.get("repo", default_repo),
        "title": pr.get("title", ""),
        "scope": "unknown",          # _enrich_prs_with_diffstat sets authoritative scope per PR
        "blast_radius": "low",       # _enrich_prs_with_diffstat sets authoritative blast_radius per PR
        "category": category,        # _enrich_prs_with_diffstat upgrades to 'file_path' confidence
        "category_confidence": category_confidence,
        "is_test_pr": flags["is_test_pr"],
        "is_wip_pr": flags["is_wip_pr"],
        "is_docs_pr": flags["is_docs_pr"],
        "pr_type": _classify_pr_type(pr, flags),
        "author": author_login,
        "age_hours": round(float(age_hours), 1),
        "branch": pr.get("headRefName", "") or pr.get("branch", ""),
        "target_branch": pr.get("target_branch", "") or pr.get("baseRefName", ""),
        "ci_status": ci_status,
        "has_approval": bool(has_approval),
        "approval_count": int(approval_count),
        "reviewer_count": int(reviewer_count),
        "url": pr.get("url", ""),
        "labels": pr.get("labels", []),
        "issue_ref": _extract_issue_ref(pr),
        "risk_score": 0,
        "risk_tier": "low",
        "risk_dimensions": {},
    }


@click.command()
@click.option("--label", default="", help="Optional label filter (empty = all open PRs; GH only)")
@click.option("--repo", default="", help="Repo override: full URL or org/repo slug")
@click.option("--target-branch", "--target", default="", envvar="TARGET_BRANCH",
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
    if target_branch and raw_prs:
        from collections import Counter
        branch_dist = Counter(p.get("target_branch", p.get("baseRefName", "?")) for p in raw_prs)
        top3 = branch_dist.most_common(3)
        print(f"[collect_ready] target_branch distribution (top 3): {dict(top3)}", flush=True)

    ready_prs = [_normalize_pr(pr, slug) for pr in raw_prs]

    # Safety filter: enforce target_branch even if upstream fetcher missed some PRs.
    # BB API server-side filtering may silently ignore the q parameter on some endpoints,
    # and pagination can return PRs from other branches on subsequent pages.
    if target_branch:
        before = len(ready_prs)
        ready_prs = [p for p in ready_prs if p.get("target_branch", "") == target_branch]
        if before != len(ready_prs):
            print(
                f"[collect_ready] target_branch safety filter: {before} → {len(ready_prs)} "
                f"(removed {before - len(ready_prs)} PRs not targeting {target_branch!r})",
                flush=True,
            )

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
