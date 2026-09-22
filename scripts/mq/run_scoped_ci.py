"""Run CI for a specific test shard (called per fan-out item).

Detects project type and runs the appropriate test command for the shard's
file list.

Usage:
    python -m scripts.mq.run_scoped_ci --shard '{"shard_id":"0","tests":"[\"tests/test_foo.py\"]"}'

Required env: (none required, CODEBASE_DIR recommended)
Reads:  shard definition from --shard JSON or /workspace/test_impact.json
Writes: /workspace/shard_result_{shard_id}.json

Exit codes: 0=all pass, 1=error, 3=test failures
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import click

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.shell import run_cmd


_PROJECT_TYPE_MARKERS = [
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("package.json", "node"),
    ("Gemfile", "ruby"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("build.gradle.kts", "kotlin"),
    ("build.gradle", "java"),
    ("pom.xml", "java"),
    ("*.sln", "csharp"),
    ("*.csproj", "csharp"),
    ("Makefile", "make"),
]


def _detect_project_type(repo_dir: str) -> str:
    """Detect project type from marker files."""
    repo_path = Path(repo_dir)
    for marker, ptype in _PROJECT_TYPE_MARKERS:
        if "*" in marker:
            if any(repo_path.glob(marker)):
                return ptype
        elif (repo_path / marker).exists():
            return ptype
    return "python"


def _build_test_command(
    project_type: str,
    tests: list[str],
    repo_dir: str,
    junit_path: str | None = None,
) -> list[str]:
    """Build the test runner command for a shard."""
    if project_type == "python":
        cmd = ["python", "-m", "pytest", "-v", "--tb=short"]
        if junit_path:
            cmd.extend([f"--junitxml={junit_path}"])
        cmd.extend(tests)
        return cmd

    if project_type == "go":
        packages: set[str] = set()
        for t in tests:
            pkg = str(Path(t).parent)
            if pkg == ".":
                pkg = "./..."
            else:
                pkg = f"./{pkg}/..."
            packages.add(pkg)
        cmd = ["go", "test", "-v", "-count=1"]
        if junit_path:
            cmd = ["gotestsum", "--junitfile", junit_path, "--"] + list(packages)
        else:
            cmd.extend(sorted(packages))
        return cmd

    if project_type == "rust":
        modules: set[str] = set()
        for t in tests:
            parts = t.split("/")
            if len(parts) >= 2 and parts[0] == "crates":
                modules.add(parts[1])
        cmd = ["cargo", "test"]
        for m in sorted(modules):
            cmd.extend(["-p", m])
        return cmd

    if project_type == "node":
        cmd = ["npx", "jest", "--verbose"]
        if junit_path:
            cmd.extend(["--reporters=jest-junit"])
        cmd.extend(["--"] + tests)
        return cmd

    if project_type == "java":
        if (Path(repo_dir) / "build.gradle").exists():
            cmd = ["./gradlew", "test", "--tests"]
            test_patterns = [t.replace("/", ".").replace(".java", "") for t in tests]
            cmd.extend(test_patterns)
        else:
            cmd = ["mvn", "test", "-pl", ",".join({str(Path(t).parent) for t in tests})]
        return cmd

    if project_type == "ruby":
        cmd = ["bundle", "exec", "rspec"]
        if junit_path:
            cmd.extend(["--format", "RspecJunitFormatter", "--out", junit_path])
        cmd.extend(tests)
        return cmd

    if project_type == "kotlin":
        cmd = ["./gradlew", "test", "--tests"]
        test_patterns = [t.replace("/", ".").replace(".kt", "") for t in tests]
        cmd.extend(test_patterns)
        return cmd

    if project_type == "csharp":
        cmd = ["dotnet", "test", "--filter"]
        test_classes = [Path(t).stem for t in tests]
        cmd.append("|".join(f"FullyQualifiedName~{c}" for c in test_classes))
        if junit_path:
            cmd.extend(["--logger", f"junit;LogFilePath={junit_path}"])
        return cmd

    return ["python", "-m", "pytest", "-v"] + tests


def _run_tests(
    cmd: list[str],
    repo_dir: str,
) -> tuple[int, str, str, float]:
    """Execute the test command and capture results."""
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=1800,
        )
        duration = time.monotonic() - start
        return result.returncode, result.stdout, result.stderr, duration
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return 1, "", "Test execution timed out after 30 minutes", duration
    except FileNotFoundError as e:
        duration = time.monotonic() - start
        return 1, "", f"Command not found: {e}", duration


def _parse_test_counts(stdout: str, stderr: str) -> tuple[int, int, int]:
    """Parse passed/failed/skipped counts from test output."""
    passed = failed = skipped = 0
    pytest_match = re.search(r"(\d+) passed", stdout + stderr)
    if pytest_match:
        passed = int(pytest_match.group(1))
    fail_match = re.search(r"(\d+) failed", stdout + stderr)
    if fail_match:
        failed = int(fail_match.group(1))
    skip_match = re.search(r"(\d+) skipped", stdout + stderr)
    if skip_match:
        skipped = int(skip_match.group(1))

    go_pass = re.search(r"ok\s+\S+\s+", stdout)
    if go_pass:
        passed = max(passed, len(re.findall(r"^ok\s+", stdout, re.MULTILINE)))
    go_fail = re.search(r"^FAIL\s+", stdout, re.MULTILINE)
    if go_fail:
        failed = max(failed, len(re.findall(r"^FAIL\s+", stdout, re.MULTILINE)))

    return passed, failed, skipped


@click.command()
@click.option("--shard", required=True, help="JSON shard definition or shard_id")
@click.option("--shard-id", default=None, help="Shard ID (if not in --shard JSON)")
def main(shard: str, shard_id: str | None) -> None:
    config = load_config(required=[])
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = config.get("CODEBASE_DIR", str(workspace / "repo"))

    try:
        shard_data = json.loads(shard)
    except json.JSONDecodeError:
        shard_data = {"shard_id": shard}

    sid = shard_data.get("shard_id", shard_id or "0")
    tests_raw = shard_data.get("tests", "[]")
    if isinstance(tests_raw, str):
        try:
            tests = json.loads(tests_raw)
        except json.JSONDecodeError:
            tests = [tests_raw]
    else:
        tests = tests_raw

    if not tests:
        impact_path = workspace / "test_impact.json"
        if impact_path.exists():
            impact = json.loads(impact_path.read_text())
            for s in impact.get("shards", []):
                if str(s.get("shard_id")) == str(sid):
                    tests = s.get("tests", [])
                    break

    print(f"[run_scoped_ci] shard={sid} tests={len(tests)}", flush=True)

    if not tests:
        print("[run_scoped_ci] no tests in shard — skipping", flush=True)
        result = {
            "shard_id": sid,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "duration_s": 0,
            "status": "skipped",
        }
        (workspace / f"shard_result_{sid}.json").write_text(json.dumps(result, indent=2))
        sys.exit(0)

    project_type = _detect_project_type(repo_dir)
    print(f"[run_scoped_ci] project_type={project_type} repo={repo_dir}", flush=True)

    junit_path = str(workspace / f"shard_{sid}_junit.xml")
    cmd = _build_test_command(project_type, tests, repo_dir, junit_path)
    print(f"[run_scoped_ci] running: {' '.join(cmd[:6])}{'...' if len(cmd) > 6 else ''}", flush=True)

    returncode, stdout, stderr, duration = _run_tests(cmd, repo_dir)
    passed, failed, skipped = _parse_test_counts(stdout, stderr)

    status = "passed" if returncode == 0 else "failed"
    print(
        f"[run_scoped_ci] shard={sid} status={status} "
        f"passed={passed} failed={failed} skipped={skipped} "
        f"duration={duration:.1f}s",
        flush=True,
    )

    if stdout:
        for line in stdout.splitlines()[-20:]:
            print(f"  {line}", flush=True)
    if stderr and returncode != 0:
        for line in stderr.splitlines()[-10:]:
            print(f"  [stderr] {line}", flush=True)

    result = {
        "shard_id": sid,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "duration_s": round(duration, 1),
        "status": status,
        "returncode": returncode,
        "junit_xml": junit_path if Path(junit_path).exists() else None,
    }

    out_path = workspace / f"shard_result_{sid}.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"[run_scoped_ci] wrote {out_path}", flush=True)

    if failed > 0:
        sys.exit(3)
    elif returncode != 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
