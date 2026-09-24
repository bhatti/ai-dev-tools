"""Run CI for a specific test shard (called per fan-out item).

Detects project type and runs the appropriate test command for the shard's
file list.  Supports Python, Go, Rust, Node/Jest, Java (Gradle/Maven),
Kotlin, Ruby, C#, and Makefile projects.  Override detection with TEST_CMD.

Usage:
    python -m scripts.mq.run_scoped_ci --shard '{"shard_id":"0","tests":"[\"tests/test_foo.py\"]"}'

Required env: (none required, CODEBASE_DIR recommended)
Optional env: TEST_CMD — override auto-detected test command (e.g. "make test")
Reads:  shard definition from --shard JSON or /workspace/test_impact.json
Writes: /workspace/shard_result_{shard_id}.json (indexed so fan-out siblings don't collide on download)

Exit codes: 0=all pass, 1=error, 3=test failures
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import click

from scripts.common.config import get_workspace_dir, load_config


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


def _detect_project_type(repo_dir: str, tests: list[str] | None = None) -> str:
    """Detect project type from marker files at the repo root.

    Falls back to inferring from shard test-file extensions when filesystem
    markers are absent (e.g. clone into a subdirectory or missing file).
    Marker priority is intentional: more specific languages first.
    """
    repo_path = Path(repo_dir)
    for marker, ptype in _PROJECT_TYPE_MARKERS:
        if "*" in marker:
            if any(repo_path.glob(marker)):
                return ptype
        elif (repo_path / marker).exists():
            return ptype

    # Filesystem check failed (wrong CWD, subdirectory clone, etc.).
    # Infer from test file extensions so Go/Rust/TS shards are never misrouted.
    if tests:
        exts = {Path(t).suffix.lower() for t in tests}
        names = {Path(t).name.lower() for t in tests}
        if any(n.endswith("_test.go") for n in names):
            return "go"
        if any(n.endswith("_test.rs") or n.endswith(".rs") for n in names):
            return "rust"
        if ".ts" in exts or ".tsx" in exts or ".js" in exts or ".jsx" in exts:
            return "node"
        if ".java" in exts:
            return "java"
        if ".kt" in exts:
            return "kotlin"
        if ".rb" in exts:
            return "ruby"
        if ".py" in exts:
            return "python"
    return "python"


def _detect_concurrency() -> int:
    """Detect available CPUs respecting cgroup limits (K8s pods).

    Reads cgroup v2 (cpu.max) then v1 (cpu.cfs_quota_us/period) to honour
    the container's CPU request/limit.  Falls back to os.cpu_count().
    """
    cgroup_max = Path("/sys/fs/cgroup/cpu.max")
    cfs_quota = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    cfs_period = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    try:
        if cgroup_max.exists():
            parts = cgroup_max.read_text().strip().split()
            if parts[0] != "max":
                return max(1, int(parts[0]) // int(parts[1]))
        elif cfs_quota.exists() and cfs_period.exists():
            quota = int(cfs_quota.read_text().strip())
            period = int(cfs_period.read_text().strip())
            if quota > 0:
                return max(1, quota // period)
    except (OSError, ValueError):
        pass
    return max(1, os.cpu_count() or 1)


def _build_test_command(
    project_type: str,
    tests: list[str],
    repo_dir: str,
    junit_path: str | None = None,
    concurrency: int | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Build the test runner command for a shard.

    Returns (command, extra_env) where extra_env contains environment
    variables needed for concurrency control.
    """
    n = concurrency or _detect_concurrency()
    extra_env: dict[str, str] = {}

    if project_type == "python":
        cmd = ["python", "-m", "pytest", "-v", "--tb=short", "--durations=5"]
        if junit_path:
            cmd.append(f"--junitxml={junit_path}")
        cmd.extend(tests)
        return cmd, extra_env

    if project_type == "go":
        packages: set[str] = set()
        for t in tests:
            pkg = str(Path(t).parent)
            packages.add("./..." if pkg == "." else f"./{pkg}/...")
        extra_env["GOMAXPROCS"] = str(n)
        if junit_path and shutil.which("gotestsum"):
            cmd = ["gotestsum", "--junitfile", junit_path, "--",
                   "-count=1", f"-parallel={n}"] + sorted(packages)
        else:
            # gotestsum not installed — fall back to plain go test
            cmd = ["go", "test", "-v", "-count=1", f"-parallel={n}"]
            cmd.extend(sorted(packages))
        return cmd, extra_env

    if project_type == "rust":
        modules: set[str] = set()
        for t in tests:
            parts = t.split("/")
            if len(parts) >= 2 and parts[0] == "crates":
                modules.add(parts[1])
        cmd = ["cargo", "test"]
        for m in sorted(modules):
            cmd.extend(["-p", m])
        cmd.extend(["--", f"--test-threads={n}"])
        extra_env["RUST_TEST_THREADS"] = str(n)
        return cmd, extra_env

    if project_type == "node":
        cmd = ["npx", "jest", "--verbose", f"--maxWorkers={n}"]
        if junit_path:
            cmd.append("--reporters=jest-junit")
        cmd.extend(["--"] + tests)
        return cmd, extra_env

    if project_type == "java":
        if (Path(repo_dir) / "build.gradle").exists():
            cmd = ["./gradlew", "test", f"--max-workers={n}", "--tests"]
            test_patterns = [t.replace("/", ".").replace(".java", "") for t in tests]
            cmd.extend(test_patterns)
        else:
            cmd = ["mvn", "test", f"-T{n}", "-pl",
                   ",".join(sorted({str(Path(t).parent) for t in tests}))]
        return cmd, extra_env

    if project_type == "ruby":
        cmd = ["bundle", "exec", "rspec"]
        if junit_path:
            cmd.extend(["--format", "RspecJunitFormatter", "--out", junit_path])
        cmd.extend(tests)
        return cmd, extra_env

    if project_type == "kotlin":
        cmd = ["./gradlew", "test", f"--max-workers={n}", "--tests"]
        test_patterns = [t.replace("/", ".").replace(".kt", "") for t in tests]
        cmd.extend(test_patterns)
        return cmd, extra_env

    if project_type == "csharp":
        cmd = ["dotnet", "test", "--filter"]
        test_classes = [Path(t).stem for t in tests]
        cmd.append("|".join(f"FullyQualifiedName~{c}" for c in test_classes))
        if junit_path:
            cmd.extend(["--logger", f"junit;LogFilePath={junit_path}"])
        extra_env["DOTNET_PROCESSOR_COUNT"] = str(n)
        return cmd, extra_env

    if project_type == "make":
        cmd = ["make", "test"]
        return cmd, extra_env

    return ["python", "-m", "pytest", "-v"] + tests, extra_env


def _run_tests(
    cmd: list[str],
    repo_dir: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str, float]:
    """Execute the test command and capture results."""
    env = None
    if extra_env:
        env = {**os.environ, **extra_env}
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=repo_dir,
            env=env,
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
    """Parse passed/failed/skipped counts from test output.

    Supports pytest, Go, Rust, Jest, RSpec, and dotnet output formats.
    """
    combined = stdout + stderr
    passed = failed = skipped = 0

    # pytest: "5 passed, 2 failed, 1 skipped in 3.2s"
    m = re.search(r"(\d+) passed", combined)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+) failed", combined)
    if m:
        failed = int(m.group(1))
    m = re.search(r"(\d+) skipped", combined)
    if m:
        skipped = int(m.group(1))

    # Go: "ok  github.com/org/repo/pkg  0.5s" / "FAIL github.com/..."
    go_ok = re.findall(r"^ok\s+", stdout, re.MULTILINE)
    go_fail = re.findall(r"^FAIL\s+", stdout, re.MULTILINE)
    if go_ok or go_fail:
        passed = max(passed, len(go_ok))
        failed = max(failed, len(go_fail))

    # Rust: "test result: ok. 5 passed; 0 failed; 1 ignored"
    m = re.search(r"test result:.*?(\d+) passed.*?(\d+) failed.*?(\d+) ignored", combined)
    if m:
        passed = max(passed, int(m.group(1)))
        failed = max(failed, int(m.group(2)))
        skipped = max(skipped, int(m.group(3)))

    # Jest: "Tests:  2 failed, 5 passed, 7 total"
    m = re.search(r"Tests:\s+(?:(\d+)\s+failed,\s*)?(\d+)\s+passed", combined)
    if m:
        if m.group(1):
            failed = max(failed, int(m.group(1)))
        passed = max(passed, int(m.group(2)))

    # RSpec: "15 examples, 2 failures, 1 pending"
    m = re.search(r"(\d+)\s+examples?,\s+(\d+)\s+failures?(?:,\s+(\d+)\s+pending)?", combined)
    if m:
        total_examples = int(m.group(1))
        rspec_failures = int(m.group(2))
        rspec_pending = int(m.group(3)) if m.group(3) else 0
        passed = max(passed, total_examples - rspec_failures - rspec_pending)
        failed = max(failed, rspec_failures)
        skipped = max(skipped, rspec_pending)

    # dotnet: "Passed!  - Failed: 0, Passed: 10, Skipped: 2, Total: 12"
    m = re.search(r"Failed:\s*(\d+),\s*Passed:\s*(\d+),\s*Skipped:\s*(\d+)", combined)
    if m:
        failed = max(failed, int(m.group(1)))
        passed = max(passed, int(m.group(2)))
        skipped = max(skipped, int(m.group(3)))

    return passed, failed, skipped


def _parse_slow_tests(stdout: str, stderr: str, top_n: int = 5) -> list[dict]:
    """Extract slowest tests from runner output.

    Supports pytest --durations, Go test -v, Rust test output.
    Returns list of {"name": str, "duration_s": float} sorted slowest first.
    """
    combined = stdout + stderr
    slow: list[dict] = []

    # pytest --durations: "1.23s call     tests/test_foo.py::test_bar"
    for m in re.finditer(r"([\d.]+)s\s+(?:call|setup)\s+(.+)", combined):
        slow.append({"name": m.group(2).strip(), "duration_s": float(m.group(1))})

    # Go: "--- PASS: TestFoo (1.230s)" or "--- FAIL: TestFoo (2.100s)"
    if not slow:
        for m in re.finditer(r"---\s+(?:PASS|FAIL):\s+(\S+)\s+\(([\d.]+)s\)", combined):
            slow.append({"name": m.group(1), "duration_s": float(m.group(2))})

    # Rust: "test module::test_name ... ok (1.23s)" — not all Rust runners emit timing
    if not slow:
        for m in re.finditer(r"test\s+(\S+)\s+\.\.\.\s+\w+\s+\(([\d.]+)s\)", combined):
            slow.append({"name": m.group(1), "duration_s": float(m.group(2))})

    slow.sort(key=lambda x: x["duration_s"], reverse=True)
    return slow[:top_n]


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

    # TEST_CMD override — lets users specify their own test runner
    test_cmd_override = os.environ.get("TEST_CMD", "").strip()

    if test_cmd_override:
        cmd = shlex.split(test_cmd_override) + tests
        extra_env: dict[str, str] = {}
        project_type = "custom"
        print(f"[run_scoped_ci] using TEST_CMD override: {test_cmd_override}", flush=True)
    else:
        project_type = _detect_project_type(repo_dir, tests)
        junit_path = str(workspace / "test_output.xml")
        cmd, extra_env = _build_test_command(project_type, tests, repo_dir, junit_path)

    print(f"[run_scoped_ci] project_type={project_type} repo={repo_dir}", flush=True)
    print(f"[run_scoped_ci] running: {' '.join(cmd[:6])}{'...' if len(cmd) > 6 else ''}", flush=True)

    returncode, stdout, stderr, duration = _run_tests(cmd, repo_dir, extra_env)
    passed, failed, skipped = _parse_test_counts(stdout, stderr)
    slow_tests = _parse_slow_tests(stdout, stderr)

    status = "passed" if returncode == 0 else "failed"
    print(
        f"[run_scoped_ci] shard={sid} status={status} "
        f"passed={passed} failed={failed} skipped={skipped} "
        f"duration={duration:.1f}s",
        flush=True,
    )

    if slow_tests:
        print(f"[run_scoped_ci] slowest tests:", flush=True)
        for st in slow_tests[:3]:
            print(f"  {st['duration_s']:.2f}s  {st['name']}", flush=True)

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
        "project_type": project_type,
        "slow_tests": slow_tests,
    }

    # Write shard_result_{sid}.json — each fan-out child writes its own indexed file so
    # that when the report task downloads all fan-out artifacts into a shared workspace,
    # files from different shards don't overwrite each other.  The report task globs
    # shard_result_*.json to aggregate across all shards.
    out_path = workspace / f"shard_result_{sid}.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"[run_scoped_ci] wrote {out_path}", flush=True)

    # Emit result as task context so FanOutTasklet can aggregate across shards.
    # The tasklet prefixes each child's context with "{item_var}_{idx}_", so the
    # parent task execution ends up with shard_0_ShardResult, shard_1_ShardResult etc.
    summary = {"shard_id": sid, "passed": passed, "failed": failed,
               "skipped": skipped, "duration_s": round(duration, 1), "status": status}
    print(f"::add-task-context ShardResult::{json.dumps(summary)}", flush=True)

    if failed > 0:
        sys.exit(3)
    elif returncode != 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
