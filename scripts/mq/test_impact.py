"""Determine the minimal test set for a PR diff — the pipeline speed knob.

Maps changed files to affected tests via naming conventions, import graph
traversal, and historical timing data for shard balancing.

Usage:
    python -m scripts.mq.test_impact --pr-number 42

Required env: GH_ORG + GH_REPO (GitHub) or BITBUCKET_WORKSPACE + BITBUCKET_REPO (Bitbucket)
Reads:  PR diff (via gh CLI or Bitbucket API), optional /workspace/test_timings.json
Writes: /workspace/test_impact.json

Exit codes: 0=done, 1=error (falls back to full suite — never blocks)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import click

from scripts.common.config import get_workspace_dir, load_config
from scripts.mq._shared import fetch_pr_files, is_test_file, repo_slug

_SKIP_DIRS = frozenset({
    "node_modules", "vendor", "target", ".git", "__pycache__",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", "dist",
    "build", ".gradle", ".next", "coverage",
})


def _should_skip(path: Path) -> bool:
    """Return True if path is under a directory we should never crawl."""
    return bool(_SKIP_DIRS & set(path.parts))


_LANG_TEST_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "python": [
        (r"^(src/)?(.+)\.py$", r"tests/test_\2.py"),
        (r"^(src/)?(.+)\.py$", r"tests/\2/test_\2.py"),
        (r"^(.+)/([^/]+)\.py$", r"\1/tests/test_\2.py"),
    ],
    "go": [
        (r"^(.+)\.go$", r"\1_test.go"),
    ],
    "typescript": [
        (r"^(src/)?(.+)\.(ts|tsx)$", r"src/__tests__/\2.test.\3"),
        (r"^(src/)?(.+)\.(ts|tsx)$", r"\2.test.\3"),
        (r"^(src/)?(.+)\.(ts|tsx)$", r"\2.spec.\3"),
    ],
    "java": [
        (r"^src/main/java/(.+)\.java$", r"src/test/java/\1Test.java"),
    ],
    "kotlin": [
        (r"^src/main/kotlin/(.+)\.kt$", r"src/test/kotlin/\1Test.kt"),
    ],
    "ruby": [
        (r"^(lib|app)/(.+)\.rb$", r"spec/\2_spec.rb"),
        (r"^(lib|app)/(.+)\.rb$", r"test/\2_test.rb"),
    ],
    "csharp": [
        (r"^(.+)/([^/]+)\.cs$", r"\1.Tests/\2Tests.cs"),
        (r"^(.+)/([^/]+)\.cs$", r"\1Tests/\2Tests.cs"),
    ],
    "rust": [
        (r"^(crates/[^/]+)/src/(.+)\.rs$", r"\1/tests/\2.rs"),
    ],
}

_IMPORT_PATTERNS: dict[str, re.Pattern] = {
    "python": re.compile(r"^\s*(?:from|import)\s+([\w.]+)", re.MULTILINE),
    "go": re.compile(r'^\s*"([^"]+)"', re.MULTILINE),
    "typescript": re.compile(r"""(?:import|require)\s*\(?['"](\.[\w./@-]+)['"]""", re.MULTILINE),
    "java": re.compile(r"^\s*import\s+([\w.]+);", re.MULTILINE),
    "kotlin": re.compile(r"^\s*import\s+([\w.]+)", re.MULTILINE),
    "ruby": re.compile(r"""^\s*require(?:_relative)?\s+['"]([\w./]+)['"]""", re.MULTILINE),
    "csharp": re.compile(r"^\s*using\s+([\w.]+);", re.MULTILINE),
    "rust": re.compile(r"^\s*use\s+([\w:]+)", re.MULTILINE),
}

def _detect_language(files: list[str]) -> str:
    """Detect primary language from file extensions."""
    ext_counts: dict[str, int] = {}
    for f in files:
        ext = Path(f).suffix.lower()
        ext_counts[ext] = ext_counts.get(ext, 0) + 1

    lang_map = {
        ".py": "python",
        ".go": "go",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "typescript",
        ".jsx": "typescript",
        ".java": "java",
        ".kt": "kotlin",
        ".rb": "ruby",
        ".cs": "csharp",
        ".rs": "rust",
    }
    best_ext = max(ext_counts, key=ext_counts.get, default="") if ext_counts else ""
    return lang_map.get(best_ext, "python")



def _map_to_test_files(
    changed_files: list[str],
    lang: str,
    repo_dir: str | None,
) -> tuple[list[str], list[str]]:
    """Map changed source files to corresponding test files.

    Returns (found_tests, unmapped_files).
    """
    patterns = _LANG_TEST_PATTERNS.get(lang, _LANG_TEST_PATTERNS["python"])
    found_tests: set[str] = set()
    unmapped: list[str] = []

    for src_file in changed_files:
        if is_test_file(src_file):
            found_tests.add(src_file)
            continue

        matched = False
        for src_pattern, test_pattern in patterns:
            m = re.match(src_pattern, src_file)
            if m:
                try:
                    test_path = re.sub(src_pattern, test_pattern, src_file)
                except re.error:
                    continue
                if repo_dir:
                    full_path = Path(repo_dir) / test_path
                    if full_path.exists():
                        found_tests.add(test_path)
                        matched = True
                        break
                else:
                    found_tests.add(test_path)
                    matched = True
                    break

        if not matched and not is_test_file(src_file):
            unmapped.append(src_file)

    return sorted(found_tests), unmapped


def _discover_dependent_tests(
    changed_files: list[str],
    lang: str,
    repo_dir: str | None,
) -> list[str]:
    """Find test files that import any of the changed modules.

    Scans one level of transitive dependents.
    """
    if not repo_dir:
        return []

    import_pattern = _IMPORT_PATTERNS.get(lang)
    if not import_pattern:
        return []

    changed_modules: set[str] = set()
    for f in changed_files:
        p = Path(f)
        changed_modules.add(p.stem)
        changed_modules.add(p.with_suffix("").as_posix().replace("/", "."))

    dependent_tests: set[str] = set()
    repo_path = Path(repo_dir)

    ext_map = {
        "python": "**/*.py",
        "go": "**/*_test.go",
        "typescript": "**/*.test.ts",
        "java": "**/*Test.java",
        "kotlin": "**/*Test.kt",
        "ruby": "**/*_spec.rb",
        "csharp": "**/*Tests.cs",
        "rust": "**/*_test.rs",
    }
    glob_pattern = ext_map.get(lang, "**/*.py")

    try:
        for test_file in repo_path.glob(glob_pattern):
            rel = test_file.relative_to(repo_path)
            if _should_skip(rel):
                continue
            if not is_test_file(str(rel)):
                continue
            try:
                content = test_file.read_text(errors="ignore")
            except OSError:
                continue
            for m in import_pattern.finditer(content):
                imported = m.group(1)
                imported_parts = re.split(r"[./:\\]", imported)
                if any(mod in imported_parts for mod in changed_modules):
                    rel_path = str(test_file.relative_to(repo_path))
                    dependent_tests.add(rel_path)
                    break
    except OSError:
        pass

    return sorted(dependent_tests)


def _partition_shards(
    tests: list[str],
    num_shards: int,
    timings: dict[str, float] | None,
) -> list[dict]:
    """Split tests into balanced shards using timing data or file size heuristic."""
    if num_shards <= 0:
        num_shards = 1
    if not tests:
        return []

    test_times: list[tuple[str, float]] = []
    for t in tests:
        if timings and t in timings:
            test_times.append((t, timings[t]))
        else:
            test_times.append((t, 10.0))

    test_times.sort(key=lambda x: x[1], reverse=True)

    shards: list[list[tuple[str, float]]] = [[] for _ in range(min(num_shards, len(tests)))]
    shard_totals = [0.0] * len(shards)

    for test, duration in test_times:
        min_idx = min(range(len(shards)), key=lambda i: shard_totals[i])
        shards[min_idx].append((test, duration))
        shard_totals[min_idx] += duration

    return [
        {
            "shard_id": i,
            "tests": [t[0] for t in shard],
            "test_count": len(shard),
            "est_duration_s": round(sum(t[1] for t in shard), 1),
        }
        for i, shard in enumerate(shards)
        if shard
    ]


def _find_all_test_files(repo_dir: str) -> list[str]:
    """Return all test file paths in the repo, skipping vendor/build dirs."""
    repo_path = Path(repo_dir)
    result: list[str] = []
    try:
        for f in repo_path.rglob("*"):
            if not f.is_file():
                continue
            try:
                rel = f.relative_to(repo_path)
            except ValueError:
                continue
            if _should_skip(rel):
                continue
            if is_test_file(str(rel)):
                result.append(str(rel))
    except OSError:
        pass
    return sorted(result)


def _count_all_tests(repo_dir: str | None) -> int:
    """Count total test files in the repo for reduction percentage."""
    if not repo_dir:
        return 0
    return len(_find_all_test_files(repo_dir))


def _load_timings(workspace: "Path") -> "dict[str, float] | None":
    timings_path = workspace / "test_timings.json"
    if timings_path.exists():
        try:
            data = json.loads(timings_path.read_text())
            print(f"[test_impact] loaded {len(data)} test timing entries", flush=True)
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _emit_results(
    workspace: "Path",
    all_selected: list[str],
    total_tests: int,
    lang: str,
    shards: list[dict],
    unmapped: list[str],
    fallback_full_suite: bool,
) -> None:
    """Write test_impact.json and emit context markers.

    TestShards is emitted as ::add-job-context (job-scoped) so that
    fan_out.source: TestShards in the calling task's YAML can resolve it.
    All other metrics use ::add-task-context (task-scoped reporting only).
    """
    reduction_pct = round((1.0 - len(all_selected) / total_tests) * 100, 1) if total_tests > 0 else 0.0
    impact = {
        "total_tests": total_tests,
        "selected_tests": len(all_selected),
        "reduction_pct": max(reduction_pct, 0.0),
        "language": lang,
        "shards": shards,
        "unmapped_files": unmapped,
        "test_files": all_selected,
        "fallback_full_suite": fallback_full_suite,
    }
    (workspace / "test_impact.json").write_text(json.dumps(impact, indent=2))
    print(
        f"[test_impact] selected {len(all_selected)}/{total_tests} tests "
        f"({impact['reduction_pct']}% reduction) across {len(shards)} shards"
        f"{' (full suite)' if fallback_full_suite else ''}",
        flush=True,
    )
    # Emit compact shard metadata as job-context so fan_out.source resolves it.
    # The full test lists are in test_impact.json (artifact), which run_scoped_ci reads
    # via its fallback path. Omitting 'tests' here keeps the message well under the 1 MB
    # formicary queue limit — a large repo can have 500 KB+ of test file paths.
    compact_shards = [
        {"shard_id": s["shard_id"], "test_count": s.get("test_count", 0),
         "est_duration_s": s.get("est_duration_s", 0)}
        for s in shards
    ]
    print(f"::add-job-context TestShards::{json.dumps(compact_shards)}", flush=True)
    # Remaining metrics are task-scoped (visible in task logs and reports).
    print(f"::add-task-context SELECTED_TESTS::{len(all_selected)}", flush=True)
    print(f"::add-task-context TOTAL_TESTS::{total_tests}", flush=True)
    print(f"::add-task-context REDUCTION_PCT::{impact['reduction_pct']}", flush=True)
    print(f"::add-task-context SHARD_COUNT::{len(shards)}", flush=True)


def _fetch_pr_base_branch(config: dict, pr_number: str) -> str | None:
    """Try to fetch the base branch of a PR from GitHub/Bitbucket.

    Returns None when pr_number is not numeric or the API call fails.
    """
    if not pr_number or not pr_number.isdigit():
        return None
    try:
        # If config has a BITBUCKET_WORKSPACE, assume Bitbucket (not yet implemented).
        if config.get("BITBUCKET_WORKSPACE"):
            return None
        import subprocess as _sp
        r = _sp.run(
            ["gh", "pr", "view", pr_number,
             "--repo", repo_slug(config),
             "--json", "baseRefName"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            return data.get("baseRefName") or None
    except Exception:
        pass
    return None


def _full_suite(workspace: "Path", repo_dir: str, num_shards: int,
                timings: "dict[str, float] | None") -> None:
    """Emit full suite results and exit 0."""
    if not repo_dir:
        print("[test_impact] no CODEBASE_DIR — cannot discover tests", flush=True)
        _emit_results(workspace, [], 0, "unknown", [], [], True)
        sys.exit(0)
    all_tests = _find_all_test_files(repo_dir)
    lang = _detect_language(all_tests) if all_tests else "unknown"
    total_tests = len(all_tests) or 1
    shards = _partition_shards(all_tests, num_shards, timings)
    print(f"[test_impact] full suite: {len(all_tests)} tests ({lang}) across {len(shards)} shards", flush=True)
    _emit_results(workspace, all_tests, total_tests, lang, shards, [], True)
    sys.exit(0)


@click.command()
@click.option("--pr-number", required=True,
              help="PR number (numeric), branch name, or tag to analyze")
@click.option("--num-shards", default=4, help="Number of parallel test shards")
@click.option("--head", default="",
              help="Head branch/commit for diff-scoped runs (e.g. 'feature/my-branch'). "
                   "Required with --diff-scope unless --pr-number is a numeric PR ID.")
@click.option("--diff-scope", is_flag=True, default=False,
              help="Scope tests to changed files only (opt-in). Requires a resolved diff "
                   "(numeric PR ID or --head). Default: always run full suite.")
def main(pr_number: str, num_shards: int, head: str, diff_scope: bool) -> None:
    """Partition the test suite into shards for parallel execution.

    Default (no --diff-scope): always run the full test suite partitioned into
    num_shards balanced shards.  Reliable — no missed tests due to indirect deps.

    With --diff-scope: scope to files changed by the PR/branch diff.
    - For a numeric PR ID (e.g. '42'): fetch changed files from GitHub/Bitbucket.
      The PR's base branch is auto-extracted; no --head needed.
    - For a branch/tag (e.g. 'feature/x'): requires --head to specify what to diff
      against (defaults to BASE_BRANCH env var when omitted).
    Always falls back to the full suite when no tests map to changed files.
    """
    config = load_config(required=[])
    slug = repo_slug(config)
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = config.get("CODEBASE_DIR", "")
    base_branch = head or config.get("BASE_BRANCH", "main")

    print(
        f"[test_impact] pr={pr_number} repo={slug} shards={num_shards} "
        f"diff_scope={diff_scope} head={head or '(auto)'}",
        flush=True,
    )

    timings = _load_timings(workspace)

    # --- Full suite mode (default) ---
    if not diff_scope:
        _full_suite(workspace, repo_dir, num_shards, timings)
        # _full_suite always sys.exit(0)

    # --- Diff-scoped mode (--diff-scope) ---
    # For a numeric PR without an explicit --head, auto-detect base branch from PR metadata.
    if pr_number.isdigit() and not head:
        detected = _fetch_pr_base_branch(config, pr_number)
        if detected:
            base_branch = detected
            print(f"[test_impact] PR #{pr_number} base branch: {base_branch}", flush=True)

    # Always apply resolved base to config so fetch_pr_files / fetch_changed_files_from_diff
    # use the correct base regardless of whether --pr-number is numeric or a branch name.
    config["BASE_BRANCH"] = base_branch

    files = fetch_pr_files(config, pr_number)
    changed_files = [f.get("path", "") for f in files if f.get("path")]

    if not changed_files:
        print("[test_impact] no changed files — falling back to full suite", flush=True)
        _full_suite(workspace, repo_dir, num_shards, timings)

    lang = _detect_language(changed_files)
    print(f"[test_impact] detected language={lang} changed_files={len(changed_files)}", flush=True)

    mapped_tests, unmapped = _map_to_test_files(changed_files, lang, repo_dir or None)
    dependent_tests = _discover_dependent_tests(changed_files, lang, repo_dir or None)

    all_selected = sorted(set(mapped_tests) | set(dependent_tests))
    fallback_full_suite = False
    if not all_selected:
        print("[test_impact] no tests mapped — falling back to full suite", flush=True)
        fallback_full_suite = True
        if repo_dir:
            all_selected = _find_all_test_files(repo_dir)
    print(
        f"[test_impact] mapped={len(mapped_tests)} dependent={len(dependent_tests)} "
        f"total_selected={len(all_selected)} unmapped={len(unmapped)}"
        f"{' (fallback: full suite)' if fallback_full_suite else ''}",
        flush=True,
    )

    total_tests = _count_all_tests(repo_dir or None) or max(len(all_selected), 1)
    shards = _partition_shards(all_selected, num_shards, timings)
    _emit_results(workspace, all_selected, total_tests, lang, shards, unmapped, fallback_full_suite)


if __name__ == "__main__":
    main()
