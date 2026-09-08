"""Git history analysis for issue root-cause archaeology and codebase-wide audit.

All operations are best-effort — any git failure returns empty output, never raises.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# ── Code-file classification ───────────────────────────────────────────────────

_EXCLUDE_PATHS = frozenset({
    ".git", "node_modules", "vendor", "__pycache__", ".cache",
    "dist", "build", "target", ".tox", ".venv", "venv",
    "coverage", ".nyc_output",
})

_NON_CODE_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".bmp",
    ".mp3", ".mp4", ".wav", ".avi", ".mov",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".jar", ".war", ".ear", ".class", ".pyc", ".pyo",
    ".exe", ".dll", ".so", ".dylib", ".a", ".o",
    ".wasm",
})

_EXCLUDE_FILENAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "gemfile.lock", "poetry.lock", "cargo.lock", "go.sum",
    "composer.lock", "pipfile.lock",
})

_CODE_SPECIAL_NAMES = frozenset({
    "makefile", "dockerfile", "jenkinsfile", "vagrantfile",
    "gemfile", "rakefile", "brewfile", "procfile", "cmakelists.txt",
})


def _is_code_file(filepath: str) -> bool:
    """Return True if filepath looks like a source code file worth analyzing.

    Excludes: binaries, media, lock files, generated files, .git internals.
    """
    parts = filepath.replace("\\", "/").split("/")
    for part in parts[:-1]:
        if part in _EXCLUDE_PATHS:
            return False

    name = parts[-1]
    lower = name.lower()

    if lower in _EXCLUDE_FILENAMES:
        return False
    if lower.endswith(".min.js") or lower.endswith(".min.css"):
        return False

    ext = Path(filepath).suffix.lower()

    if not ext:
        return lower in _CODE_SPECIAL_NAMES

    if ext in _NON_CODE_EXTENSIONS:
        return False
    return True


def _run_git(args: list[str], cwd: Path, timeout: int = 120) -> str:
    """Run a git command; return stdout or '' on any failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _related_commits(repo_path: Path, issue_key: str, n: int = 10) -> list[dict]:
    """Find commits whose message references issue_key."""
    out = _run_git(
        ["log", "--oneline", f"-{n}", f"--grep={issue_key}",
         "--format=%h|%s|%an|%ad", "--date=short"],
        repo_path,
    )
    commits = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({
                "hash": parts[0],
                "message": parts[1],
                "author": parts[2],
                "date": parts[3],
            })
    return commits


def _hot_files(repo_path: Path, n_commits: int = 50) -> list[tuple[str, int]]:
    """Return (filepath, change_count) for the top 50 most-changed files in last n_commits."""
    out = _run_git(["log", "--name-only", "--pretty=format:", f"-{n_commits}"], repo_path)
    counts: dict[str, int] = {}
    for line in out.splitlines():
        line = line.strip()
        if line and _is_code_file(line):
            counts[line] = counts.get(line, 0) + 1
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)[:50]


def _file_volatility(repo_path: Path, files: list[str], n: int = 50) -> dict[str, int]:
    """Return {filepath: commit_count} for the given files over last n commits."""
    result = {}
    for f in files:
        out = _run_git(["log", "--oneline", f"-{n}", "--", f], repo_path)
        result[f] = len([ln for ln in out.splitlines() if ln.strip()])
    return result


def _recent_changes(repo_path: Path, files: list[str], n: int = 10) -> list[dict]:
    """Return last n commits touching any of the given files."""
    if not files:
        return []
    args = ["log", "--format=%h|%s|%an|%ad", "--date=short", f"-{n}", "--"] + files
    out = _run_git(args, repo_path)
    commits = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({
                "hash": parts[0],
                "message": parts[1],
                "author": parts[2],
                "date": parts[3],
            })
    return commits


def extract_stats(context: str) -> dict:
    """Parse summary statistics from a build_context() result.

    Returns: commits_found, hot_files count, top_hot_file string.
    """
    import re
    if not context:
        return {"commits_found": 0, "hot_files": 0, "top_hot_file": ""}
    commits = len(re.findall(r"^- [0-9a-f]{7}\b", context, re.MULTILINE))
    hot_matches = re.findall(r"^- (.+): (\d+) changes", context, re.MULTILINE)
    top = f"{hot_matches[0][0]} ({hot_matches[0][1]} changes)" if hot_matches else ""
    return {"commits_found": commits, "hot_files": len(hot_matches), "top_hot_file": top}


def get_repo_info(repo_path: Path, n_commits: int = 0) -> dict:
    """Return branch, HEAD commit, author, date, and optionally oldest commit in the analyzed range."""
    repo_path = Path(repo_path)
    info = {
        "branch": _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path),
        "head_commit": _run_git(["log", "-1", "--format=%h"], repo_path),
        "head_author": _run_git(["log", "-1", "--format=%an"], repo_path),
        "head_date": _run_git(["log", "-1", "--format=%ad", "--date=short"], repo_path),
    }
    if n_commits > 0:
        # Oldest commit in the analyzed range (N commits back from HEAD)
        oldest_hash = _run_git(["log", f"-{n_commits}", "--format=%h"], repo_path)
        if oldest_hash:
            lines = oldest_hash.splitlines()
            info["oldest_commit"] = lines[-1].strip() if lines else ""
        oldest_date = _run_git(["log", f"-{n_commits}", "--format=%ad", "--date=short"], repo_path)
        if oldest_date:
            lines = oldest_date.splitlines()
            info["oldest_date"] = lines[-1].strip() if lines else ""
    return info


def build_context(repo_path: Path, issue_keys: list[str], n: int = 10) -> str:
    """Return a Markdown block with git history context for the given issue keys.

    Returns empty string if the repo path is invalid or all queries produce no data.
    """
    repo_path = Path(repo_path)
    if not (repo_path / ".git").exists():
        return ""

    sections: list[str] = ["## Git History Context\n"]

    # Related commits per issue key
    for key in issue_keys:
        if not key:
            continue
        commits = _related_commits(repo_path, key, n)
        if commits:
            sections.append(f"### Commits mentioning {key}")
            for c in commits:
                sections.append(f"- {c['hash']} {c['message']} ({c['author']}, {c['date']})")
            sections.append("")

    # Hot files across last 50 commits
    hot = _hot_files(repo_path, n_commits=50)
    if hot:
        sections.append("### File volatility (change count, last 50 commits)")
        for filepath, count in hot[:5]:
            label = " (hot file)" if count >= 10 else ""
            sections.append(f"- {filepath}: {count} changes{label}")
        sections.append("")

        # Recent changes to the hottest files
        hot_paths = [f for f, _ in hot[:3]]
        recent = _recent_changes(repo_path, hot_paths, n)
        if recent:
            sections.append("### Recent changes to hot files")
            for c in recent:
                sections.append(f"- {c['hash']} {c['message']} ({c['author']}, {c['date']})")
            sections.append("")

    if len(sections) <= 1:
        return ""  # no useful data found

    result = "\n".join(sections)
    # Cap output to avoid oversized prompts on large repos with deep history.
    if len(result) > 3000:
        result = result[:3000] + "\n... (truncated)"
    return result


# ── Codebase-wide audit functions ─────────────────────────────────────────────
# All are best-effort: return empty/default on any git failure, never raise.


def analyze_commit_range(
    repo_path: Path,
    n_commits: int = 1000,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Return structured commit list for the last n_commits in the repo.

    Each entry: {hash, message, author, date, files: [str], lines_added: int,
                 lines_removed: int}.
    """
    repo_path = Path(repo_path)
    if not (repo_path / ".git").exists():
        return []

    # Build git log command: custom format marker + numstat on same stream
    args = [
        "log",
        f"--format=COMMIT:%H|%s|%an|%ad",
        "--date=short",
        "--numstat",
        f"-{n_commits}",
    ]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")

    out = _run_git(args, repo_path)
    if not out:
        return []

    commits: list[dict] = []
    current: dict | None = None
    for line in out.splitlines():
        if line.startswith("COMMIT:"):
            if current is not None:
                commits.append(current)
            parts = line[len("COMMIT:"):].split("|", 3)
            current = {
                "hash": parts[0] if len(parts) > 0 else "",
                "message": parts[1] if len(parts) > 1 else "",
                "author": parts[2] if len(parts) > 2 else "",
                "date": parts[3] if len(parts) > 3 else "",
                "files": [],
                "lines_added": 0,
                "lines_removed": 0,
            }
        elif current is not None and line.strip():
            # numstat line: "added\tremoved\tpath"
            parts = line.split("\t", 2)
            if len(parts) == 3:
                filepath = parts[2].strip()
                if filepath and _is_code_file(filepath):
                    current["files"].append(filepath)
                try:
                    current["lines_added"] += int(parts[0]) if parts[0] != "-" else 0
                    current["lines_removed"] += int(parts[1]) if parts[1] != "-" else 0
                except ValueError:
                    pass

    if current is not None:
        commits.append(current)
    return commits


def compute_temporal_coupling(
    commits: list[dict],
    min_support: int = 5,
    min_confidence: float = 0.5,
) -> list[dict]:
    """Return file pairs that co-change across commits, excluding same-directory pairs.

    Returns [{file_a, file_b, co_changes, confidence}] sorted by confidence desc.
    confidence = co_changes / min(count_a, count_b).
    """
    if not commits:
        return []

    # Build {file: set of commit hashes}
    file_hashes: dict[str, set[str]] = {}
    for c in commits:
        h = c.get("hash", "")
        for f in c.get("files", []):
            file_hashes.setdefault(f, set()).add(h)

    # For large codebases, cap to the 500 most-changed files to keep O(n²) tractable.
    # Files changed in fewer commits are unlikely to produce meaningful temporal coupling.
    if len(file_hashes) > 500:
        file_hashes = dict(
            sorted(file_hashes.items(), key=lambda x: len(x[1]), reverse=True)[:500]
        )

    files = list(file_hashes.keys())
    results: list[dict] = []

    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            a, b = files[i], files[j]
            # Only cross-directory pairs
            if os.path.dirname(a) == os.path.dirname(b):
                continue
            co = len(file_hashes[a] & file_hashes[b])
            if co < min_support:
                continue
            conf = co / min(len(file_hashes[a]), len(file_hashes[b]))
            if conf < min_confidence:
                continue
            results.append({"file_a": a, "file_b": b, "co_changes": co, "confidence": round(conf, 3)})

    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results[:30]


def compute_knowledge_silos(
    repo_path: Path,
    files: list[str],
    n_commits: int = 200,
) -> dict[str, dict]:
    """Return per-file author concentration for the given files.

    Returns {filepath: {top_author, top_author_pct, total_commits, unique_authors}}.
    """
    repo_path = Path(repo_path)
    result: dict[str, dict] = {}
    for f in files:
        out = _run_git(["log", "--format=%an", f"-{n_commits}", "--", f], repo_path, timeout=15)
        if not out:
            continue
        authors = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if not authors:
            continue
        counts: dict[str, int] = {}
        for a in authors:
            counts[a] = counts.get(a, 0) + 1
        top_author = max(counts, key=counts.__getitem__)
        total = len(authors)
        result[f] = {
            "top_author": top_author,
            "top_author_pct": round(counts[top_author] / total, 3),
            "total_commits": total,
            "unique_authors": len(counts),
        }
    return result


def compute_commit_health(commits: list[dict]) -> dict:
    """Return aggregate commit quality metrics.

    Returns {total, fix_ratio, large_commit_count, vague_message_count,
             avg_files_per_commit, ai_coauthored_count}.
    """
    if not commits:
        return {
            "total": 0, "fix_ratio": 0.0, "large_commit_count": 0,
            "vague_message_count": 0, "avg_files_per_commit": 0.0, "ai_coauthored_count": 0,
        }

    _fix_prefixes = {"fix", "fix:", "bug", "bug:", "bugfix", "hotfix", "patch", "revert"}
    total = len(commits)
    fix_count = 0
    large_count = 0
    vague_count = 0
    ai_count = 0
    file_counts: list[int] = []

    for c in commits:
        msg = c.get("message", "")
        first_word = msg.lower().split()[0] if msg.strip() else ""
        if first_word in _fix_prefixes:
            fix_count += 1
        if len(c.get("files", [])) > 15:
            large_count += 1
        if len(msg.strip()) < 10:
            vague_count += 1
        msg_lower = msg.lower()
        if "co-authored-by: claude" in msg_lower or "co-authored-by: copilot" in msg_lower:
            ai_count += 1
        file_counts.append(len(c.get("files", [])))

    return {
        "total": total,
        "fix_ratio": round(fix_count / total, 3),
        "large_commit_count": large_count,
        "vague_message_count": vague_count,
        "avg_files_per_commit": round(sum(file_counts) / total, 2),
        "ai_coauthored_count": ai_count,
    }


def find_test_gaps(
    commits: list[dict],
    test_patterns: list[str] | None = None,
) -> dict:
    """Return test coverage gaps from commit history.

    Returns {untested_files, brittle_test_files, test_debt_indicators}.
    """
    if test_patterns is None:
        test_patterns = ["test_", "_test.", ".test.", "spec.", ".spec.", "_spec."]

    def _is_test(path: str) -> bool:
        return any(p in path for p in test_patterns)

    # Build per-file commit-hash sets and test-file churn counts
    prod_file_commits: dict[str, list[str]] = {}
    test_file_counts: dict[str, int] = {}
    test_debt_indicators = 0
    _debt_keywords = ("todo test", "skip test", "xfail", "no test", "add test", "fix test")

    for c in commits:
        h = c.get("hash", "")
        msg = c.get("message", "").lower()
        if any(kw in msg for kw in _debt_keywords):
            test_debt_indicators += 1
        commit_tests = {f for f in c.get("files", []) if _is_test(f)}
        for f in c.get("files", []):
            if _is_test(f):
                test_file_counts[f] = test_file_counts.get(f, 0) + 1
            else:
                prod_file_commits.setdefault(f, []).append(h)

    # untested_files: prod changed ≥3 times, none of those commits changed a test file
    untested: list[str] = []
    for prod_file, hashes in prod_file_commits.items():
        if len(hashes) < 3:
            continue
        # Check if any of these commits also changed a test file
        touched_test = False
        for c in commits:
            if c.get("hash", "") in hashes:
                if any(_is_test(f) for f in c.get("files", [])):
                    touched_test = True
                    break
        if not touched_test:
            untested.append(prod_file)

    # brittle_test_files: test churn > 2× inferred production counterpart
    brittle: list[str] = []
    for test_file, test_count in test_file_counts.items():
        # Infer prod counterpart: strip test prefix/suffix and directory
        base = os.path.basename(test_file)
        for pfx in ("test_",):
            if base.startswith(pfx):
                base = base[len(pfx):]
        for sfx in ("_test.py", "_spec.py", ".test.ts", ".spec.ts", "_test.go", "_test.js"):
            if base.endswith(sfx):
                base = base[: -len(sfx)] + sfx[sfx.rfind("."):]
                break
        prod_count = prod_file_commits.get(base, [])
        if not prod_count:
            # Search partial match
            for prod_f, hashes in prod_file_commits.items():
                if base.split(".")[0] in os.path.basename(prod_f):
                    prod_count = hashes
                    break
        if len(prod_count) > 0 and test_count > 2 * len(prod_count):
            brittle.append(test_file)

    return {
        "untested_files": sorted(untested[:50]),
        "brittle_test_files": sorted(brittle[:25]),
        "test_debt_indicators": test_debt_indicators,
    }


def build_audit_context(
    repo_path: Path,
    n_commits: int = 1000,
    focus: str = "all",
    max_code_size: int = 1_048_576,
    max_context_chars: int = 100_000,
) -> str:
    """Run all audit dimensions and return a structured Markdown context block.

    focus: "all" | "architecture" | "security" | "tests" | "duplicates" | "health"
    max_context_chars: cap on total output size (default 100k — Claude handles 200k+ context).
    Returns "" if the path has no .git directory.
    """
    repo_path = Path(repo_path)
    if not (repo_path / ".git").exists():
        return ""

    _focus_all = focus == "all"

    print(f"[audit] analyzing last {n_commits} commits (focus={focus}, max_code_size={max_code_size // 1024}KB) ...", flush=True)
    commits = analyze_commit_range(repo_path, n_commits=n_commits)
    print(f"[audit] parsed {len(commits)} commits", flush=True)

    sections: list[str] = [f"## Repository Audit Context (last {len(commits)} commits)\n"]
    sections.append(f"_Max code size for analysis: {max_code_size // 1024}KB — focus on hotspot files first._\n")

    # --- Hotspot Analysis (always included) ---
    hot = _hot_files(repo_path, n_commits=n_commits)
    if hot:
        sections.append("## Hotspot Analysis")
        sections.append(f"Top files by change count (threshold for HOT: >{max(1, n_commits // 20)} changes):")
        for filepath, count in hot:  # all 50 returned by _hot_files
            hot_flag = " [HOT]" if count > max(1, n_commits // 20) else ""
            pct = round(count * 100 / max(len(commits), 1), 1)
            sections.append(f"- {filepath}: {count} changes ({pct}%){hot_flag}")
        sections.append("")

    # --- Temporal Coupling ---
    if _focus_all or focus == "architecture":
        coupling = compute_temporal_coupling(commits, min_support=min(5, max(2, len(commits) // 50)))
        if coupling:
            sections.append("## Temporal Coupling")
            sections.append("File pairs that frequently change together (cross-module — hidden coupling risk):")
            for pair in coupling:  # show all 30 returned by compute_temporal_coupling
                sections.append(
                    f"- {pair['file_a']} ↔ {pair['file_b']}: "
                    f"{pair['co_changes']} co-changes, confidence={pair['confidence']}"
                )
            sections.append("")

    # --- Knowledge Silos ---
    if _focus_all or focus == "health":
        if hot:
            top_files = [f for f, _ in hot[:25]]
            silos = compute_knowledge_silos(repo_path, top_files, n_commits=min(n_commits, 500))
            if silos:
                sections.append("## Knowledge Silos")
                sections.append("Author concentration in hotspot files (top 25):")
                for filepath, info in silos.items():
                    flag = " [SILO RISK]" if info["top_author_pct"] > 0.8 else ""
                    sections.append(
                        f"- {filepath}: {info['top_author']} wrote {int(info['top_author_pct']*100)}% "
                        f"of {info['total_commits']} commits ({info['unique_authors']} unique authors){flag}"
                    )
                sections.append("")

    # --- Commit Health ---
    if _focus_all or focus == "health":
        health = compute_commit_health(commits)
        if health.get("total", 0) > 0:
            sections.append("## Commit Health")
            sections.append(f"- Total commits: {health['total']}")
            sections.append(f"- Fix: commit ratio: {int(health['fix_ratio']*100)}%"
                            + (" [HIGH]" if health['fix_ratio'] > 0.4 else ""))
            sections.append(f"- Avg files/commit: {health['avg_files_per_commit']}")
            sections.append(f"- Large commits (>15 files): {health['large_commit_count']}")
            sections.append(f"- Vague messages (<10 chars): {health['vague_message_count']}")
            if health["ai_coauthored_count"]:
                sections.append(f"- AI co-authored commits: {health['ai_coauthored_count']}")
            sections.append("")

    # --- Test Health ---
    if _focus_all or focus == "tests":
        gaps = find_test_gaps(commits)
        sections.append("## Test Health")
        sections.append(f"- Untested production files (changed ≥3× with no test changes): "
                        f"{len(gaps['untested_files'])}")
        if gaps["untested_files"]:
            for f in gaps["untested_files"][:30]:
                sections.append(f"  - {f}")
        sections.append(f"- Brittle test files (churn >2× production): "
                        f"{len(gaps['brittle_test_files'])}")
        if gaps["brittle_test_files"]:
            for f in gaps["brittle_test_files"][:15]:
                sections.append(f"  - {f}")
        sections.append(f"- Test debt commit messages (skip/xfail/todo test): "
                        f"{gaps['test_debt_indicators']}")
        sections.append("")

    # --- Bug Hotspots (files recurring in fix/bug commits) ---
    if _focus_all or focus == "health":
        bug_hotspots = _bug_hotspot_files(repo_path)
        if bug_hotspots:
            sections.append("## Bug Hotspots (files in fix/bug commits)")
            sections.append("Files most frequently touched by commits mentioning fix/bug/broken:")
            for filepath, count in bug_hotspots[:30]:
                sections.append(f"- {filepath}: {count} bug-related commits")
            sections.append("")

    # --- Commit Velocity (monthly distribution) ---
    if _focus_all or focus == "health":
        velocity = _commit_velocity(repo_path)
        if velocity:
            sections.append("## Commit Velocity (monthly)")
            for month, count in velocity[-12:]:  # last 12 months
                sections.append(f"- {month}: {count} commits")
            sections.append("")

    # --- Emergency / Revert Commits ---
    if _focus_all or focus == "health":
        emergency = _emergency_commits(repo_path)
        if emergency:
            sections.append("## Emergency / Revert Commits (last year)")
            for line in emergency[:25]:
                sections.append(f"- {line}")
            sections.append("")

    # --- Top Contributors ---
    if _focus_all or focus == "health":
        contributors = _top_contributors(repo_path, n_commits=n_commits)
        if contributors:
            sections.append("## Top Contributors")
            for author, count in contributors[:10]:
                sections.append(f"- {author}: {count} commits")
            sections.append("")

    if len(sections) <= 1:
        return ""

    result = "\n".join(sections)
    if len(result) > max_context_chars:
        # Truncate at a section boundary where possible
        cut = result[:max_context_chars]
        last_section = cut.rfind("\n## ")
        if last_section > max_context_chars // 2:
            cut = cut[:last_section]
        result = cut + f"\n\n... (context truncated at {max_context_chars // 1000}k chars — {len(result) // 1000}k total available; focus on high-change-count files first)"
    return result


def _bug_hotspot_files(repo_path: Path, max_commits: int = 2000) -> list[tuple[str, int]]:
    """Files most frequently appearing in fix/bug/broken commits."""
    out = _run_git(
        ["log", f"-{max_commits}", "-i", "-E", "--grep=fix|bug|broken",
         "--name-only", "--format=", "--since=2 years ago"],
        repo_path,
    )
    counts: dict[str, int] = {}
    for line in out.splitlines():
        line = line.strip()
        if line and _is_code_file(line):
            counts[line] = counts.get(line, 0) + 1
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)


def _commit_velocity(repo_path: Path, max_commits: int = 5000) -> list[tuple[str, int]]:
    """Monthly commit counts — useful for spotting velocity spikes/crashes."""
    out = _run_git(
        ["log", f"-{max_commits}", "--format=%ad", "--date=format:%Y-%m"],
        repo_path,
    )
    counts: dict[str, int] = {}
    for line in out.splitlines():
        line = line.strip()
        if line:
            counts[line] = counts.get(line, 0) + 1
    return sorted(counts.items())


def _emergency_commits(repo_path: Path, max_commits: int = 2000) -> list[str]:
    """Commits with revert/hotfix/emergency/rollback in subject line (last year)."""
    out = _run_git(
        ["log", f"-{max_commits}", "--oneline", "--since=1 year ago"],
        repo_path,
    )
    results = []
    for line in out.splitlines():
        lower = line.lower()
        if any(kw in lower for kw in ("revert", "hotfix", "emergency", "rollback")):
            results.append(line.strip())
    return results


def _top_contributors(repo_path: Path, n_commits: int = 1000) -> list[tuple[str, int]]:
    """Top committers by commit count (excluding merges)."""
    out = _run_git(
        ["log", "--no-merges", f"-{n_commits}", "--format=%an"],
        repo_path,
    )
    counts: dict[str, int] = {}
    for line in out.splitlines():
        author = line.strip()
        if author:
            counts[author] = counts.get(author, 0) + 1
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)
