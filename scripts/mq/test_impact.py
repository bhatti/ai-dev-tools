"""Determine the minimal test set for a PR diff — the pipeline speed knob.

Maps changed files to affected tests via naming conventions, import graph
traversal, and historical timing data for shard balancing.

Usage:
    python -m scripts.mq.test_impact --pr-number 42

Required env: GH_ORG, GH_REPO
Reads:  PR diff (via gh CLI), optional /workspace/test_timings.json
Writes: /workspace/test_impact.json

Exit codes: 0=done, 1=error (falls back to full suite — never blocks)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import click

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.shell import run_cmd
from scripts.mq._shared import is_test_file


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


_is_test_file = is_test_file


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
        if _is_test_file(src_file):
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

        if not matched and not _is_test_file(src_file):
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
        stem = Path(f).stem
        module = f.replace("/", ".").replace(".py", "").replace(".go", "").replace(".ts", "").replace(".java", "").replace(".kt", "").replace(".rb", "").replace(".cs", "").replace(".rs", "")
        changed_modules.add(stem)
        changed_modules.add(module)

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
            if not _is_test_file(str(test_file.relative_to(repo_path))):
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


def _count_all_tests(repo_dir: str | None) -> int:
    """Count total test files in the repo for reduction percentage."""
    if not repo_dir:
        return 0

    count = 0
    repo_path = Path(repo_dir)
    try:
        for f in repo_path.rglob("*"):
            if f.is_file():
                try:
                    rel = str(f.relative_to(repo_path))
                except ValueError:
                    continue
                if is_test_file(rel):
                    count += 1
    except OSError:
        pass
    return count


@click.command()
@click.option("--pr-number", required=True, help="PR number to analyze")
@click.option("--num-shards", default=4, help="Number of test shards")
def main(pr_number: str, num_shards: int) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO"])
    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = config.get("CODEBASE_DIR", "")

    print(f"[test_impact] pr={pr_number} repo={org}/{repo} shards={num_shards}", flush=True)

    result = run_cmd([
        "gh", "pr", "view", pr_number,
        "--repo", f"{org}/{repo}",
        "--json", "files",
    ])
    pr_data = json.loads(result.stdout)
    changed_files = [f.get("path", "") for f in pr_data.get("files", []) if f.get("path")]

    if not changed_files:
        print("[test_impact] no changed files — nothing to test", flush=True)
        impact = {
            "total_tests": 0,
            "selected_tests": 0,
            "reduction_pct": 100.0,
            "shards": [],
            "unmapped_files": [],
        }
        (workspace / "test_impact.json").write_text(json.dumps(impact, indent=2))
        sys.exit(0)

    lang = _detect_language(changed_files)
    print(f"[test_impact] detected language={lang} changed_files={len(changed_files)}", flush=True)

    mapped_tests, unmapped = _map_to_test_files(changed_files, lang, repo_dir or None)
    dependent_tests = _discover_dependent_tests(changed_files, lang, repo_dir or None)

    all_selected = sorted(set(mapped_tests) | set(dependent_tests))
    print(
        f"[test_impact] mapped={len(mapped_tests)} dependent={len(dependent_tests)} "
        f"total_selected={len(all_selected)} unmapped={len(unmapped)}",
        flush=True,
    )

    timings: dict[str, float] | None = None
    timings_path = workspace / "test_timings.json"
    if timings_path.exists():
        try:
            timings = json.loads(timings_path.read_text())
            print(f"[test_impact] loaded {len(timings)} test timing entries", flush=True)
        except (json.JSONDecodeError, OSError):
            pass

    shards = _partition_shards(all_selected, num_shards, timings)
    total_tests = _count_all_tests(repo_dir or None) or max(len(all_selected), 1)
    reduction_pct = round((1.0 - len(all_selected) / total_tests) * 100, 1) if total_tests > 0 else 0.0

    impact = {
        "total_tests": total_tests,
        "selected_tests": len(all_selected),
        "reduction_pct": max(reduction_pct, 0.0),
        "language": lang,
        "shards": shards,
        "unmapped_files": unmapped,
        "test_files": all_selected,
    }

    out_path = workspace / "test_impact.json"
    out_path.write_text(json.dumps(impact, indent=2))
    print(
        f"[test_impact] selected {len(all_selected)}/{total_tests} tests "
        f"({impact['reduction_pct']}% reduction) across {len(shards)} shards",
        flush=True,
    )

    fan_out_value = json.dumps([
        {"shard_id": str(s["shard_id"]), "tests": json.dumps(s["tests"])}
        for s in shards
    ])
    print(f"::add-task-context TEST_GROUPS::{fan_out_value}", flush=True)


if __name__ == "__main__":
    main()
