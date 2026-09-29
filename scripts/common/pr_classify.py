"""Shared PR classification and risk scoring for all report pipelines.

Extracted from scripts/mq/collect_ready.py so that MQ, PR-queue, and PR-audit
pipelines all use the same taxonomy, formulas, and emoji constants.

Public API
----------
classify_pr_type        — bug / feature / refactor / chore / security / test / docs / unknown
classify_pr_category    — domain bucket (security, api, ui, …) with confidence level
classify_pr_flags       — boolean flags: is_test_pr, is_wip_pr, is_docs_pr
compute_risk_score      — multi-dimension risk model → score + tier
apply_blast_cap         — cap blast_radius when PR is test-only / docs-only / WIP
extract_issue_ref       — Jira or GitHub issue reference from PR text
compute_complexity      — LOC×files bucketed to low / medium / high
compute_is_hotspot      — True when PR touches sensitive paths
enrich_pr_with_metrics  — one-call enrichment: sets all fields in-place
build_category_breakdown        — markdown table for category distribution
build_work_type_distribution    — markdown table for PR type distribution
"""
from __future__ import annotations

import os
import re
from collections import Counter
from datetime import datetime, timezone

# Shared helpers re-exported from pr_metadata (single source of truth).
# Import here at module level — no circular risk since pr_metadata imports
# pr_classify only lazily inside function bodies (enrich_pr_fast, enrich_pr_full).
from scripts.common.pr_metadata import (
    compute_pr_age as _compute_pr_age_base,
    format_pr_status,  # noqa: F401 — re-exported; callers import from pr_classify
    _size_bucket as _size_bucket_fn,
)

# ---------------------------------------------------------------------------
# Sensitive-path detection (moved from _shared.py — generic, not MQ-specific)
# ---------------------------------------------------------------------------

SENSITIVE_PATHS = re.compile(
    r"(^|/)("
    r"auth|security|billing|payments|crypto|secrets|credentials"
    r"|\.env|migrations|rbac|iam|oauth|tokens"
    r")(/|$|\.)",
    re.IGNORECASE,
)


def is_test_file(path: str) -> bool:
    """Check if a file path looks like a test file."""
    name = path.split("/")[-1] if "/" in path else path
    return (
        name.startswith("test_")
        or name.endswith("_test.go")
        or name.endswith("_test.py")
        or name.endswith("_test.rs")
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".test.js")
        or name.endswith(".test.jsx")
        or name.endswith("Test.java")
        or name.endswith("Test.kt")
        or name.endswith("_spec.rb")
        or name.endswith("_test.rb")
        or name.endswith("Tests.cs")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.js")
        or "/tests/" in path
        or "/__tests__/" in path
        or "/test/" in path
        or "/spec/" in path
    )


# ---------------------------------------------------------------------------
# Keyword regexes — compiled once at import time
# ---------------------------------------------------------------------------

_BUG_KEYWORDS = re.compile(
    r'(?:'
    r'\b(?:fix(?:e[ds])?|bug|hotfix|hot.?fix|patch(?:e[ds])?|defect|regression|crash(?:e[ds])?'
    r'|revert(?:ed|ing)?|rollback|roll.?back|roll.?forward|workaround|broken'
    r'|flak(?:y|iness|e)|investigat(?:e|ing))\b'
    r'|should\s+not\b|shouldn.t\b'
    r'|not\s+(?:work|upgrad|display|show|render|load|connect|respond|start|runn)\w*\b'
    r'|(?:can|may)\s+drop\b'
    r'|silently\s+(?:skip|fail|drop|ignore)\w*'
    r')',
    re.IGNORECASE,
)
_FEAT_KEYWORDS = re.compile(
    r'\b(feat|feature|story|enhancement|implement(?:ed|ing|s)?|add(?:ed|ing|s)?'
    r'|creat(?:e[ds]?|ing)|introduc(?:e[ds]?|ing)|support(?:ed|ing|s)?'
    r'|enabl(?:e[ds]?|ing)|integrat(?:e[ds]?|ing)|allow(?:ed|ing|s)?|provid(?:e[ds]?|ing))\b',
    re.IGNORECASE,
)
_REFACTOR_KEYWORDS = re.compile(
    r'\b(refactor(?:ed|ing|s)?|cleanup|clean.?up|detangle|extract(?:ed|ing|s)?'
    r'|reorganize|restructure|simplify|split|rename[ds]?|move[ds]?'
    r'|remov(?:e[ds]?|ing)|delet(?:e[ds]?|ing)|replac(?:e[ds]?|ing)'
    r'|optimiz(?:e[ds]?|ing)|improv(?:e[ds]?|ing)|rework(?:ed|ing)?'
    r'|consolidat(?:e[ds]?|ing)|deprecat(?:e[ds]?|ing)|decouple[ds]?'
    r'|reclassif(?:y|ied|ying))\b',
    re.IGNORECASE,
)
_CHORE_KEYWORDS = re.compile(
    r'\b(chore|deps?|dependency|upgrade[ds]?|bump(?:ed|ing|s)?|update[ds]?|version|migrate[ds]?)\b',
    re.IGNORECASE,
)
_SECURITY_KEYWORDS = re.compile(
    r'\b(vulnerability|cve|rce|ssrf|xss|injection|exploit|0-?day|zero.?day|security.?fix)\b',
    re.IGNORECASE,
)
_DOCS_TITLE_KEYWORDS = re.compile(
    r'\b(doc(?:s|umentation)?|readme|changelog|license|contributing)\b',
    re.IGNORECASE,
)
_TEST_TITLE_KEYWORDS = re.compile(
    r'\b(test(?:s|ing)?|spec(?:s)?|e2e|unit.?test|integration.?test|sdet|qa'
    r'|flak(?:y|iness|e))\b',
    re.IGNORECASE,
)

_TEST_EXACT_LABELS = frozenset({
    # "test" (bare) is intentionally excluded: Jira uses it for issue type "Test"
    # and components named "test", which means "needs testing", not "this PR adds tests".
    # Use compound labels or title keywords to detect test PRs.
    "tests", "testing", "flaky", "flaky-test", "flaky test",
    "qa", "e2e", "e2e-test", "unit-test", "integration-test", "sdet", "spec",
})

_CONVENTIONAL_COMMIT_RE = re.compile(
    r'^(?:[\[\]A-Z0-9_-]+\s+)?'
    r'(feat|fix|docs|chore|refactor|test|ci|style|perf|build|revert)'
    r'[\(:\s]',
    re.IGNORECASE,
)
_CONVENTIONAL_TYPE_MAP: dict[str, str] = {
    "feat": "feature", "fix": "bug", "docs": "docs", "chore": "chore",
    "refactor": "refactor", "test": "test", "ci": "chore", "style": "refactor",
    "perf": "refactor", "build": "chore", "revert": "bug",
}

_JIRA_KEY_RE = re.compile(r'\b([A-Z][A-Z0-9]{1,9}-\d+)\b')
_GH_CLOSES_RE = re.compile(r'(?:closes?|fixes?|resolves?)\s+#(\d+)', re.IGNORECASE)
_GH_PR_URL_RE = re.compile(r'/pull/\d+.*')

# ---------------------------------------------------------------------------
# PR flag detection — env-var configurable title/branch patterns
# ---------------------------------------------------------------------------


def _compile_opt(env_key: str, default: str) -> re.Pattern | None:
    """Compile regex from env var; return None when env var is empty string."""
    pattern = os.environ.get(env_key, default)
    return re.compile(pattern, re.IGNORECASE) if pattern else None


_TEST_TITLE_RE: re.Pattern | None = _compile_opt(
    "TEST_PR_TITLE_PATTERNS", r"\[SDET\]|\bSDET\b|\[QA\]|\bQA\b"
)
_TEST_BRANCH_PREFIXES: tuple[str, ...] = tuple(
    p.strip()
    for p in os.environ.get("TEST_BRANCH_PREFIXES", "sdet/,test/,tests/,qa/,e2e/").split(",")
    if p.strip()
)
_WIP_TITLE_RE: re.Pattern | None = _compile_opt(
    "WIP_PR_TITLE_PATTERNS", r"\[WIP\]|\bWIP\b|^DRAFT[:/]|\[PoC\]|\[POC\]"
)
_DOCS_TITLE_RE: re.Pattern | None = _compile_opt(
    "DOCS_PR_TITLE_PATTERNS", r"^docs?\(|^docs?:|^\[docs?\]"
)

_TEST_FILE_RE = re.compile(
    r"(/__tests__/|/[Tt]ests?/|\.test\.[jt]sx?$|\.spec\.[jt]sx?$"
    r"|_test\.go$|[Tt]est\.java$|(?:^|/)test_[^/]+\.py$|[^/]+_test\.py$)",
    re.IGNORECASE,
)
_DOCS_FILE_RE = re.compile(r"\.md$|\.rst$|\.txt$|/docs?/", re.IGNORECASE)
_ASSET_FILE_RE = re.compile(r"\.svg$|/icons?/|/assets?/", re.IGNORECASE)

_TEST_FILE_THRESHOLD = float(os.environ.get("TEST_FILE_THRESHOLD", "0.8"))

# ---------------------------------------------------------------------------
# Blast / risk constants
# ---------------------------------------------------------------------------

_BLAST_ORDER = ["low", "medium", "high"]
_PR_FLAG_BLAST_CAPS: dict[str, str] = {
    "is_test_pr": "low",
    "is_docs_pr": "low",
    "is_wip_pr":  "medium",
}

_SECURITY_TITLE_RE: re.Pattern | None = _compile_opt(
    "SECURITY_TITLE_KEYWORDS",
    r"\b(vulnerability|cve|rce|injection|ssrf|xss|exploit|0-day|zero-day)\b",
)

_CATEGORY_BLAST_MODIFIERS: dict[str, int] = {
    "security": 2, "authn_authz": 2,
    "sre": 1, "data": 1,
}

_RISK_WEIGHTS: dict[str, float] = {
    "size": 1.5, "file_count": 1.0, "blast_radius": 2.0,
    "sensitive_paths": 2.5, "test_coverage": 1.5, "historical": 1.0,
}

_RISK_TIER_LOW_MAX = 15
_RISK_TIER_MEDIUM_MAX = 30

# ---------------------------------------------------------------------------
# Category rules — order matters: first match wins
# ---------------------------------------------------------------------------

_CATEGORY_RULES: list[tuple[str, list[str], list[str], list[str]]] = [
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
                    [r"\bsecurity\b", r"\bcve\b", r"\bvuln", r"\btls\b", r"\bssl\b",
                     r"\bcertificates?\b"]),
    ("sre",         [r"terraform", r"infra", r"k8s", r"kubernetes", r"helm", r"deploy", r"ansible", r"packer"],
                    ["terraform", "infra", "sre", "ops"],
                    [r"\bterraform\b", r"\binfra\b", r"\bk8s\b", r"\bdeploy\b"]),
    ("data",        [r"migration", r"schema", r"database", r"db/", r"sql", r"redis", r"kafka", r"etl"],
                    ["migration", "database", "schema"],
                    [r"\bmigration\b", r"\bschema\b", r"\bdatabase\b"]),
    ("api",         [r"api/", r"route", r"handler", r"controller", r"endpoint", r"grpc", r"proto"],
                    ["api", "grpc"],
                    [r"\bapi\b", r"\bendpoint\b", r"\broutes?\b", r"\bhandler\b"]),
    ("ui",          [r"frontend", r"web/", r"ui/", r"component", r"\.tsx?", r"\.vue", r"\.svelte", r"\.css", r"\.scss"],
                    ["frontend", "ui", "ux"],
                    [r"\bui\b", r"\bfrontend\b", r"\bcomponent\b", r"\bgrid\b",
                     r"\bfont\b", r"\bwebkit\b"]),
    ("config",      [r"config", r"\.ya?ml", r"\.toml", r"\.env", r"settings"],
                    ["config", "configuration"],
                    [r"\bconfig\b", r"\bsettings\b"]),
    ("backend",     [r"src/", r"pkg/", r"lib/", r"service", r"core/"],
                    [],
                    [r"\bworkers?\b", r"\bsockets?\b", r"\bserver\b",
                     r"\bprovisioning?\b", r"\brollout\b", r"\bbatch\b",
                     r"\bpipelines?\b", r"\bqueues?\b"]),
]

# ---------------------------------------------------------------------------
# Display constants — shared across all report renderers
# ---------------------------------------------------------------------------

HIGH_BLAST_CATEGORIES = frozenset({"security", "authn_authz", "sre", "data"})

PR_TYPE_EMOJI: dict[str, str] = {
    "bug": "🐛", "feature": "✨", "refactor": "♻️", "chore": "🔧",
    "security": "🔒", "test": "🧪", "docs": "📝", "unknown": "❓",
}

RISK_EMOJI: dict[str, str] = {"high": "🔴", "medium": "🟡", "low": "🟢"}

# ---------------------------------------------------------------------------
# Classification functions
# ---------------------------------------------------------------------------


def classify_pr_flags(pr: dict, files: list[dict] | None = None) -> dict[str, bool]:
    """Return boolean flags describing special PR types.

    Keys: is_test_pr, is_wip_pr, is_docs_pr.
    Normalizes field names across BB/GH/Jira PR schemas.
    """
    title = pr.get("title", "")
    branch = pr.get("branch", "") or pr.get("headRefName", "") or pr.get("source_branch", "") or pr.get("head_ref", "")
    flags: dict[str, bool] = {"is_test_pr": False, "is_wip_pr": False, "is_docs_pr": False}

    if files:
        total = len(files)
        test_count = sum(1 for f in files if _TEST_FILE_RE.search(f.get("path", "")))
        if total and test_count / total >= _TEST_FILE_THRESHOLD:
            flags["is_test_pr"] = True
        if total and all(_DOCS_FILE_RE.search(f.get("path", "")) for f in files):
            flags["is_docs_pr"] = True
        if total and all(_ASSET_FILE_RE.search(f.get("path", "")) for f in files):
            flags["is_docs_pr"] = True

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


def apply_blast_cap(blast_radius: str, flags: dict[str, bool]) -> str:
    """Apply the lowest blast_radius cap for any active flag."""
    cap_idx = _BLAST_ORDER.index(blast_radius)
    for flag, cap in _PR_FLAG_BLAST_CAPS.items():
        if flags.get(flag):
            cap_idx = min(cap_idx, _BLAST_ORDER.index(cap))
    return _BLAST_ORDER[cap_idx]


def classify_pr_category(pr: dict, files: list[dict] | None = None) -> tuple[str, str]:
    """Classify PR into a domain category.

    Returns (category, confidence) where confidence is:
      'file_path' — derived from actual changed file paths (most reliable)
      'label'     — derived from PR labels
      'title'     — derived from PR title/description text
      'unknown'   — no signal found
    """
    if files:
        source_files = [f for f in files if not _TEST_FILE_RE.search(f.get("path", ""))]
        test_ratio = 1.0 - (len(source_files) / len(files))

        if test_ratio >= _TEST_FILE_THRESHOLD:
            return "test", "file_path"

        classify_files = source_files if source_files else files
        paths_str = " ".join(f.get("path", "") for f in classify_files).lower()
        for category, path_patterns, _, _ in _CATEGORY_RULES:
            if category == "test":
                continue
            if any(re.search(p, paths_str) for p in path_patterns):
                return category, "file_path"

    labels = _normalize_labels(pr)
    label_str = " ".join(labels).lower()
    for category, _, label_keywords, _ in _CATEGORY_RULES:
        if any(k in label_str for k in label_keywords):
            return category, "label"

    text = f"{pr.get('title', '')} {pr.get('description', '')}".lower()
    for category, _, _, title_pats in _CATEGORY_RULES:
        if title_pats and any(re.search(p, text) for p in title_pats):
            return category, "title"

    return "unknown", "unknown"


def classify_pr_type(pr: dict, flags: dict[str, bool] | None = None) -> str:
    """Classify PR into work type: bug, feature, refactor, chore, security, test, docs, feature.

    Priority: security keywords > labels > conventional commit prefix > test/doc keywords
              > bug/feat/refactor/chore title keywords > flags > branch keywords
              > description keywords > feature (default).

    jira_summary is included alongside title so Jira-linked PRs are classified from issue text.
    Test keywords are checked before bug keywords so "[Flaky Test]" → test, not bug.
    """
    title = pr.get("title", "")
    # Include Jira issue summary so Jira-linked PRs classify from issue text, not bare key
    jira_summary = pr.get("jira_summary", "") or ""
    description = pr.get("description", "") or pr.get("body", "") or ""
    # Combined text for security scan; individual checks use title or jira_summary as appropriate
    full_text = f"{title} {jira_summary} {description}"

    if _SECURITY_KEYWORDS.search(full_text):
        return "security"

    labels = _normalize_labels(pr)
    label_str = " ".join(labels).lower()

    if any(k in label_str for k in ("bug", "fix", "hotfix", "defect")):
        return "bug"
    if any(k in label_str for k in ("feature", "feat", "story", "enhancement")):
        return "feature"
    if any(k in label_str for k in ("refactor", "cleanup", "tech-debt", "tech_debt")):
        return "refactor"
    if any(k in label_str for k in ("chore", "deps", "dependency", "maintenance")):
        return "chore"
    # Compound/specific test labels are definitive (e.g. "flaky-test", "qa", "e2e").
    if any(lbl.lower() in _TEST_EXACT_LABELS for lbl in labels):
        return "test"
    # Bare "test" label requires title corroboration: Jira uses "test" as an issue type
    # for exploratory testing tasks unrelated to writing tests. Only classify as "test" when
    # the title/jira_summary also signals test work (e.g. "fix flaky assertion" → test, but
    # "Consider how... might be made fast" with Jira type "Test" → stays feature/bug).
    if any(lbl.lower() == "test" for lbl in labels):
        search_text_corr = f"{title} {jira_summary}".strip()
        if _TEST_TITLE_KEYWORDS.search(search_text_corr):
            return "test"

    cc_match = _CONVENTIONAL_COMMIT_RE.match(title)
    if cc_match:
        return _CONVENTIONAL_TYPE_MAP.get(cc_match.group(1).lower(), "feature")

    # Classify from combined title+jira_summary; test before bug so "[Flaky Test]" → test
    search_text = f"{title} {jira_summary}".strip()
    if _DOCS_TITLE_KEYWORDS.search(search_text):
        return "docs"
    if _TEST_TITLE_KEYWORDS.search(search_text):
        return "test"
    if _BUG_KEYWORDS.search(search_text):
        return "bug"
    if _FEAT_KEYWORDS.search(search_text):
        return "feature"
    if _REFACTOR_KEYWORDS.search(search_text):
        return "refactor"
    if _CHORE_KEYWORDS.search(search_text):
        return "chore"

    if flags:
        if flags.get("is_test_pr"):
            return "test"
        if flags.get("is_docs_pr"):
            return "docs"

    branch = (pr.get("branch", "") or pr.get("headRefName", "") or pr.get("source_branch", "") or pr.get("head_ref", "")).lower()
    if branch:
        if _TEST_TITLE_KEYWORDS.search(branch):
            return "test"
        if _BUG_KEYWORDS.search(branch):
            return "bug"
        if _FEAT_KEYWORDS.search(branch):
            return "feature"
        if _REFACTOR_KEYWORDS.search(branch):
            return "refactor"
        if _CHORE_KEYWORDS.search(branch):
            return "chore"

    if description:
        if _TEST_TITLE_KEYWORDS.search(description):
            return "test"
        if _BUG_KEYWORDS.search(description):
            return "bug"
        if _FEAT_KEYWORDS.search(description):
            return "feature"
        if _REFACTOR_KEYWORDS.search(description):
            return "refactor"
        if _CHORE_KEYWORDS.search(description):
            return "chore"

    return "feature"


def compute_risk_score(pr: dict, files: list[dict] | None = None) -> dict:
    """Compute multi-dimension risk score.

    Returns {"risk_score": float, "risk_tier": str, "risk_dimensions": {dim: int}}.
    Works without files (graceful degradation using PR metadata only).
    """
    dims: dict[str, int] = {}

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

    blast = pr.get("blast_radius", "low")
    blast_score = {"low": 2, "medium": 5, "high": 9}.get(blast, 2)
    category = pr.get("category", "unknown")
    blast_score = min(10, blast_score + _CATEGORY_BLAST_MODIFIERS.get(category, 0))
    dims["blast_radius"] = blast_score

    sensitive_count = 0
    if files:
        sensitive_count = sum(
            1 for f in files
            if SENSITIVE_PATHS.search(f.get("path", "")) and not is_test_file(f.get("path", ""))
        )
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
        dims["test_coverage"] = 3

    dims["historical"] = 3

    score = sum(dims[k] * _RISK_WEIGHTS[k] for k in dims)
    if score <= _RISK_TIER_LOW_MAX:
        tier = "low"
    elif score <= _RISK_TIER_MEDIUM_MAX:
        tier = "medium"
    else:
        tier = "high"

    return {"risk_score": round(score, 1), "risk_tier": tier, "risk_dimensions": dims}


def extract_issue_ref(pr: dict) -> dict | None:
    """Extract Jira or GitHub issue reference from PR title/description."""
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


def compute_complexity(total_loc: int, file_count: int) -> str:
    """Bucket LOC × files product into low / medium / high."""
    cx = total_loc * file_count
    if cx > 5000:
        return "high"
    if cx > 500:
        return "medium"
    return "low"


def compute_is_hotspot(risk_dimensions: dict) -> bool:
    """True when PR touches sensitive paths."""
    return risk_dimensions.get("sensitive_paths", 0) > 0


def enrich_pr_with_metrics(pr: dict, files: list[dict] | None = None) -> None:
    """One-call in-place enrichment: sets all classification fields on ``pr``.

    Works for BB, GH, and Jira-devstatus PR dicts — normalizes field names internally.
    When ``files`` is None, classification degrades gracefully to title/label signals.
    """
    flags = classify_pr_flags(pr, files=files)
    pr["is_test_pr"] = flags["is_test_pr"]
    pr["is_wip_pr"] = flags["is_wip_pr"]
    pr["is_docs_pr"] = flags["is_docs_pr"]
    pr["pr_type"] = classify_pr_type(pr, flags)

    if flags["is_test_pr"]:
        pr["category"] = "test"
        pr["category_confidence"] = "file_path" if files else "title"
    else:
        cat, confidence = classify_pr_category(pr, files=files)
        pr["category"] = cat
        pr["category_confidence"] = confidence

    total_loc = 0
    n_files = 0
    if files:
        total_loc = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
        n_files = len(files)
    pr["total_loc"] = total_loc
    pr["file_count"] = n_files

    blast = pr.get("blast_radius", "low")
    pr["blast_radius"] = apply_blast_cap(blast, flags)

    risk = compute_risk_score(pr, files)
    pr["risk_score"] = risk["risk_score"]
    pr["risk_tier"] = risk["risk_tier"]
    pr["risk_dimensions"] = risk["risk_dimensions"]

    pr["complexity"] = compute_complexity(total_loc, n_files)
    pr["is_hotspot"] = compute_is_hotspot(risk["risk_dimensions"])


# ---------------------------------------------------------------------------
# Summary table builders — shared across MQ, PR-queue, PR-audit reports
# ---------------------------------------------------------------------------

_TYPE_ORDER = ["feature", "bug", "security", "refactor", "chore", "test", "docs", "unknown"]
_TYPE_SIGNALS: dict[str, str] = {
    "bug": "defect indicator",
    "security": "defect indicator (security)",
    "feature": "new functionality",
    "refactor": "tech debt reduction",
    "chore": "maintenance / KTLO",
    "test": "quality investment",
    "docs": "documentation",
    "unknown": "unclassified",
}


def build_category_breakdown(prs: list[dict], lanes: list[dict] | None = None) -> list[str]:
    """Build markdown lines for Category Breakdown table.

    ``lanes`` is optional MQ-specific lane metadata (ignored when absent).
    """
    if not prs:
        return []

    cat_counts = Counter(p.get("category", "unknown") for p in prs)
    bug_by_cat = Counter(
        p.get("category", "unknown") for p in prs if p.get("pr_type") == "bug"
    )

    hotspot_cats: set[str] = set()
    if lanes:
        for lane in lanes:
            hotspot_cats.update(lane.get("hotspots", []))
    hotspot_cats.update(cat for cat, cnt in bug_by_cat.items() if cnt >= 3)

    known_cats = {c: n for c, n in cat_counts.items() if c != "unknown"}
    if not known_cats:
        return []

    high_blast_cats = {p.get("category", "unknown") for p in prs if p.get("risk_tier", p.get("blast_radius")) == "high"}

    lines: list[str] = [
        "### Category Breakdown",
        "",
        "| Category | PRs | Bug PRs | High-Blast | Hotspot |",
        "|----------|-----|---------|------------|---------|",
    ]
    for cat in sorted(known_cats, key=lambda c: -cat_counts[c]):
        n = cat_counts[cat]
        bugs = bug_by_cat.get(cat, 0)
        is_high = "⚠️" if cat in high_blast_cats or cat in HIGH_BLAST_CATEGORIES else "—"
        is_hotspot = "🔥 yes" if cat in hotspot_cats else "—"
        lines.append(f"| {cat} | {n} | {bugs} | {is_high} | {is_hotspot} |")
    unknown_n = cat_counts.get("unknown", 0)
    if unknown_n:
        lines.append(f"| unknown | {unknown_n} | {bug_by_cat.get('unknown', 0)} | — | — |")
    lines.append("")

    if hotspot_cats:
        lines.append(f"> 🔥 Hotspots (≥3 bug PRs in category): **{', '.join(sorted(hotspot_cats))}**")
        lines.append("")

    return lines


def build_work_type_distribution(prs: list[dict]) -> list[str]:
    """Build markdown lines for Work Type Distribution table."""
    total = len(prs)
    if total == 0:
        return []

    type_counts = Counter(p.get("pr_type", "unknown") for p in prs)
    if not any(t != "unknown" for t in type_counts):
        return []

    lines: list[str] = [
        "### Work Type Distribution",
        "",
        "| Type | Count | % | Signal |",
        "|------|-------|---|--------|",
    ]
    for t in _TYPE_ORDER:
        n = type_counts.get(t, 0)
        if n == 0:
            continue
        pct = round(n / total * 100, 1)
        signal = _TYPE_SIGNALS.get(t, "")
        emoji = PR_TYPE_EMOJI.get(t, "")
        lines.append(f"| {emoji} {t} | {n} | {pct}% | {signal} |")
    lines.append("")

    return lines


def _compute_pr_age_inline(pr: dict) -> int:
    """Days since created_at; falls back to age_hours for MQ PRs that lack created_at."""
    age = _compute_pr_age_base(pr)
    if age == 0:
        age_hours = pr.get("age_hours", 0)
        return int(age_hours / 24) if age_hours else 0
    return age


def _ci_emoji(ci_status: str) -> str:
    return {"pass": "✅", "fail": "❌", "pending": "⏳"}.get(ci_status, "-")


def build_pr_metrics_table(prs: list[dict]) -> list[str]:
    """Build per-PR metrics table with canonical column set.

    Columns: Status | # | PR | Title | Category | Type | Blast | Risk | LOC | Files | CI | Age | Reviewers | Issues

    Sorted by risk_score descending so highest-risk PRs appear first.
    Backward-compatible: missing fields (ci_status, age_days, reviewer_count) use safe defaults.
    """
    if not prs:
        return []

    sorted_prs = sorted(prs, key=lambda p: -p.get("risk_score", 0))

    lines: list[str] = [
        "### Per-PR Metrics",
        "",
        "| Status | # | PR | Title | Category | Type | Blast | Risk | LOC | Files | CI | Age | Reviewers | Issues |",
        "|--------|---|-----|-------|----------|------|-------|------|-----|-------|-----|-----|-----------|--------|",
    ]
    for idx, p in enumerate(sorted_prs, 1):
        pr_num = p.get("pr_number", p.get("number", p.get("id", "?")))
        pr_url = p.get("url", "")
        pr_cell = f"[#{pr_num}]({pr_url})" if pr_url else f"#{pr_num}"

        title = (p.get("title") or "").replace("|", "\\|").replace("\n", " ")
        if len(title) > 80:
            title = title[:77] + "..."

        cat = p.get("category", "—")
        hotspot_prefix = "🔥 " if p.get("is_hotspot") else ""
        cat_cell = f"{hotspot_prefix}{cat}"
        pr_type = p.get("pr_type", "—")
        type_emoji = PR_TYPE_EMOJI.get(pr_type, "")
        blast = p.get("blast_radius", "—")
        risk_score = p.get("risk_score", 0)
        risk_tier = p.get("risk_tier", "—")
        risk_emoji = RISK_EMOJI.get(risk_tier, "")
        loc = p.get("total_loc", 0)
        files = p.get("file_count", 0)

        # New columns — safe defaults for backward compatibility
        ci_status = p.get("ci_status", "unknown")
        ci_cell = _ci_emoji(ci_status)
        age_days = p.get("age_days") if p.get("age_days") is not None else _compute_pr_age_inline(p)
        age_cell = f"{age_days}d" if age_days else "—"
        reviewer_count = p.get("reviewer_count", len(p.get("reviewers") or []))
        rev_cell = str(reviewer_count) if reviewer_count else "—"
        linked = p.get("linked_issue") or {}
        issue_key = linked.get("key", "") if isinstance(linked, dict) else str(linked)
        issue_url = linked.get("url", "") if isinstance(linked, dict) else ""
        issue_cell = f"[{issue_key}]({issue_url})" if issue_url and issue_key else (issue_key or "—")

        status = format_pr_status(p)
        # Short form for table (strip emoji but keep text)
        status_short = status.split(" ", 1)[1] if " " in status else status

        lines.append(
            f"| {status_short} | {idx} | {pr_cell} | {title} "
            f"| {cat_cell} | {type_emoji} {pr_type} "
            f"| {blast} | {risk_emoji} {risk_score:.0f} | {loc:,} | {files} "
            f"| {ci_cell} | {age_cell} | {rev_cell} | {issue_cell} |"
        )
    lines.append("")
    return lines


def build_metrics_dashboard(prs: list[dict], extra_rows: list | None = None) -> str:
    """Build a portable Metrics Dashboard markdown table.

    Standard rows are computed from the PR list. Workflow-specific rows
    can be appended via extra_rows: list of (metric, value, benchmark, signal, description) tuples.

    Returns an empty string when prs is empty.
    """
    if not prs:
        return ""

    total = len(prs)
    lines: list[str] = [
        "### Metrics Dashboard",
        "",
        "| Metric | Value | Benchmark | Signal | Description |",
        "|--------|-------|-----------|--------|-------------|",
    ]

    # PR State Breakdown
    merged = sum(1 for p in prs if p.get("merged_at") or p.get("state", "").lower() == "merged")
    open_count = sum(1 for p in prs if p.get("state", "").lower() in ("open", "") and not p.get("merged_at"))
    pending = sum(1 for p in prs if not p.get("has_substantive_review") and p.get("state", "").lower() == "open")
    wip = sum(1 for p in prs if p.get("is_wip_pr"))
    declined = sum(1 for p in prs if p.get("state", "").lower() in ("declined", "closed") and not p.get("merged_at"))
    state_val = f"merged={merged} open={open_count} pending={pending} wip={wip} declined={declined} total={total}"
    lines.append(f"| PR State Breakdown | {state_val} | — | — | State distribution across the batch. Declined PRs excluded from gap-rate denominators. |")

    # Avg PR Size
    locs = [p.get("total_loc", p.get("additions", 0) + p.get("deletions", 0)) for p in prs]
    avg_loc = round(sum(locs) / total) if total else 0
    loc_signal = "🟢" if avg_loc < 300 else ("🟡" if avg_loc < 700 else "🔴")
    lines.append(f"| Avg PR Size | {avg_loc} LOC | <300 | {loc_signal} | Mean additions+deletions per PR |")

    # Size Distribution
    size_counts: dict[str, int] = {"xs": 0, "s": 0, "m": 0, "l": 0, "xl": 0}
    for p in prs:
        bucket = p.get("size_bucket", "")
        if not bucket:
            loc = p.get("total_loc", p.get("additions", 0) + p.get("deletions", 0))
            bucket = _size_bucket_fn(loc)
        size_counts[bucket] = size_counts.get(bucket, 0) + 1
    size_val = " ".join(f"{k}={v}" for k, v in size_counts.items() if v > 0)
    lines.append(f"| Size Distribution | {size_val} | — | — | PR size buckets: xs<50 s<200 m<500 l<1000 xl≥1000 LOC |")

    # Blast Radius Distribution
    blast_counts: dict[str, int] = Counter(p.get("blast_radius", "low") for p in prs)
    blast_val = f"low={blast_counts.get('low', 0)} medium={blast_counts.get('medium', 0)} high={blast_counts.get('high', 0)}"
    critical_count = blast_counts.get("critical", 0)
    if critical_count:
        blast_val += f" critical={critical_count}"
    blast_signal = "🟢" if blast_counts.get("high", 0) + critical_count <= total * 0.1 else "🟡" if blast_counts.get("high", 0) + critical_count <= total * 0.3 else "🔴"
    lines.append(f"| Blast Radius Distribution | {blast_val} | high≤10% | {blast_signal} | How broadly each PR affects the codebase |")

    # CI Pass Rate
    ci_known = [p for p in prs if p.get("ci_status") not in ("unknown", None, "", "none")]
    if ci_known:
        ci_pass = sum(1 for p in ci_known if p.get("ci_status") == "pass")
        ci_pct = round(ci_pass / len(ci_known) * 100, 1)
        ci_signal = "🟢" if ci_pct >= 90 else ("🟡" if ci_pct >= 75 else "🔴")
        lines.append(f"| CI Pass Rate | {ci_pass}/{len(ci_known)} ({ci_pct}%) | ≥90% | {ci_signal} | Fraction of PRs where all CI checks passed |")
    else:
        lines.append(f"| CI Pass Rate | N/A | ≥90% | — | CI status not available from bulk API |")

    # Review Coverage
    reviewable = [p for p in prs if not p.get("is_bot_authored") and p.get("state", "").lower() != "declined"]
    if reviewable:
        substantive = sum(1 for p in reviewable if p.get("has_substantive_review"))
        cov_pct = round(substantive / len(reviewable) * 100, 1)
        cov_signal = "🟢" if cov_pct >= 80 else ("🟡" if cov_pct >= 60 else "🔴")
        lines.append(f"| Review Coverage | {substantive}/{len(reviewable)} ({cov_pct}%) | ≥80% | {cov_signal} | PRs with at least one substantive human review |")

    # Rubber-Stamp Rate
    approved_prs = [p for p in prs if p.get("approvers") or p.get("review_decision") == "APPROVED"]
    if approved_prs:
        rubber_stamp = sum(1 for p in approved_prs if p.get("rubber_stamp_approvers"))
        rs_pct = round(rubber_stamp / len(approved_prs) * 100, 1)
        rs_signal = "🟢" if rs_pct <= 10 else ("🟡" if rs_pct <= 30 else "🔴")
        lines.append(f"| Rubber-Stamp Rate | {rubber_stamp}/{len(approved_prs)} ({rs_pct}%) | ≤10% | {rs_signal} | Approvals with zero substantive review comments |")

    # Stale PRs
    stale = sum(1 for p in prs if _compute_pr_age_inline(p) > 7)
    stale_signal = "🟢" if stale == 0 else ("🟡" if stale <= total * 0.2 else "🔴")
    lines.append(f"| Stale PRs (>7d) | {stale}/{total} | 0 | {stale_signal} | PRs open longer than 7 days |")

    # Hotspot Count
    hotspots = sum(1 for p in prs if p.get("is_hotspot"))
    hotspot_signal = "🟢" if hotspots == 0 else ("🟡" if hotspots <= 2 else "🔴")
    lines.append(f"| Hotspot Count | {hotspots}/{total} | 0 | {hotspot_signal} | PRs touching sensitive paths (auth/billing/crypto) |")

    # Avg Age
    ages = [_compute_pr_age_inline(p) for p in prs]
    avg_age = round(sum(ages) / total) if total else 0
    age_signal = "🟢" if avg_age <= 3 else ("🟡" if avg_age <= 7 else "🔴")
    lines.append(f"| Avg Age | {avg_age}d | ≤3d | {age_signal} | Mean days since PR was opened |")

    # Workflow-specific extra rows
    if extra_rows:
        for row in extra_rows:
            if len(row) >= 5:
                metric, value, benchmark, signal, description = row[0], row[1], row[2], row[3], row[4]
                lines.append(f"| {metric} | {value} | {benchmark} | {signal} | {description} |")
            elif len(row) == 4:
                lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | |")

    lines.append("")
    return "\n".join(lines)


def build_stale_pr_table(prs: list[dict], threshold_days: int = 7) -> list[str]:
    """Build a markdown table of PRs older than threshold_days.

    Columns: PR | Title | Author | Age | Status | Risk
    Sorted by age descending (oldest first).
    """
    stale = [p for p in prs if _compute_pr_age_inline(p) > threshold_days]
    if not stale:
        return []

    stale_sorted = sorted(stale, key=lambda p: -_compute_pr_age_inline(p))

    lines: list[str] = [
        f"### Stale PRs (>{threshold_days}d)",
        "",
        f"**{len(stale)}** PR{'s' if len(stale) != 1 else ''} older than {threshold_days} days",
        "",
        "| PR | Title | Author | Age | Status | Risk |",
        "|----|-------|--------|-----|--------|------|",
    ]
    for p in stale_sorted[:20]:
        pr_num = p.get("pr_number", p.get("number", p.get("id", "?")))
        pr_url = p.get("url", "")
        pr_cell = f"[#{pr_num}]({pr_url})" if pr_url else f"#{pr_num}"
        title = (p.get("title") or "")[:50]
        author = p.get("author", "—")
        age_days = _compute_pr_age_inline(p)
        age_emoji = "⛔ " if age_days >= 60 else ("🔴 " if age_days >= 30 else ("🟡 " if age_days >= 14 else ""))
        age_cell = f"{age_emoji}{age_days}d"
        _status_full = format_pr_status(p)
        status = _status_full.split(" ", 1)[1] if " " in _status_full else _status_full
        risk_tier = p.get("risk_tier", "low")
        risk_emoji = RISK_EMOJI.get(risk_tier, "")
        lines.append(f"| {pr_cell} | {title} | {author} | {age_cell} | {status} | {risk_emoji} {risk_tier} |")

    if len(stale) > 20:
        lines.append(f"| | _+{len(stale) - 20} more stale PRs_ | | | | |")
    lines.append("")
    return lines


def build_open_pr_dashboard(prs: list[dict]) -> str:
    """Full open-PR dashboard grouped by risk tier.

    Sections:
    - HIGH risk group (pr_metrics_table)
    - MEDIUM risk group
    - LOW risk group
    - Stale PRs (>7d)
    - Category Breakdown
    - Work Type Distribution
    - Metrics Dashboard
    """
    if not prs:
        return "_No open PRs found._\n"

    lines: list[str] = []

    # Group by risk tier
    high = [p for p in prs if p.get("risk_tier") == "high"]
    medium = [p for p in prs if p.get("risk_tier") == "medium"]
    low = [p for p in prs if p.get("risk_tier") not in ("high", "medium")]

    if high:
        lines.append(f"## 🔴 High Risk ({len(high)} PRs)")
        lines.append("")
        lines.extend(build_pr_metrics_table(high))
    if medium:
        lines.append(f"## 🟡 Medium Risk ({len(medium)} PRs)")
        lines.append("")
        lines.extend(build_pr_metrics_table(medium))
    if low:
        lines.append(f"## 🟢 Low Risk ({len(low)} PRs)")
        lines.append("")
        lines.extend(build_pr_metrics_table(low))

    lines.extend(build_stale_pr_table(prs, threshold_days=7))
    lines.extend(build_category_breakdown(prs))
    lines.extend(build_work_type_distribution(prs))
    lines.append(build_metrics_dashboard(prs))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_labels(pr: dict) -> list[str]:
    """Extract label strings from PR dict (handles both BB and GH label formats)."""
    raw = pr.get("labels", []) or pr.get("pr_labels", []) or []
    return [la.get("name", la) if isinstance(la, dict) else str(la) for la in raw]
