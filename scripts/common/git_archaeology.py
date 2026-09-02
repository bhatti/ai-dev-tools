"""Git history analysis for issue root-cause archaeology.

All operations are best-effort — any git failure returns empty output, never raises.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _run_git(args: list[str], cwd: Path) -> str:
    """Run a git command; return stdout or '' on any failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
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
    """Return (filepath, change_count) for the top 10 most-changed files in last n_commits."""
    out = _run_git(["log", "--name-only", "--pretty=format:", f"-{n_commits}"], repo_path)
    counts: dict[str, int] = {}
    for line in out.splitlines():
        line = line.strip()
        if line:
            counts[line] = counts.get(line, 0) + 1
    return sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]


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


def get_repo_info(repo_path: Path) -> dict:
    """Return branch, HEAD commit, author, and date for a cloned repo."""
    repo_path = Path(repo_path)
    return {
        "branch": _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path),
        "head_commit": _run_git(["log", "-1", "--format=%h"], repo_path),
        "head_author": _run_git(["log", "-1", "--format=%an"], repo_path),
        "head_date": _run_git(["log", "-1", "--format=%ad", "--date=short"], repo_path),
    }


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
