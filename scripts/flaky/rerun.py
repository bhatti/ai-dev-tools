"""Rerun a test many times and decide whether it is flaky, with evidence.

Strategy ("smart rerun"):
  1. A few *full-file* runs first (about 20% of the budget). They expose suite-level pollution that a
     lone test would hide.
  2. The remaining runs target the failing test alone when phase 1 saw it fail, otherwise the full file.
  3. Early stop on a proven mix of pass+fail, on repeated identical failures, or on an environment
     failure (which is not a verdict about the test).

Verdicts: FLAKY, DETERMINISTIC_FAIL, CANNOT_REPRODUCE, INCONCLUSIVE (too few runs), ENV_BLOCKED. Counts exclude environment failures
and runs that executed zero tests (a green exit with no tests is *not* a pass).

Usage:
    python3 -m scripts.flaky.rerun --cwd DIR --runs 20 --cmd "npx jest a.test.js" \
        [--isolate-cmd "npx jest a.test.js -t {test}"] [--parallel 4] [--env TZ=UTC] --out runs.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from scripts.common.env_scrub import scrubbed_environ

DEFAULT_RUNS = 20
FULL_RUN_RATIO = 0.2
DETERMINISTIC_FAIL_THRESHOLD = 5
MIN_RUNS_FOR_NEGATIVE = 10  # fewer completed runs cannot support a CANNOT_REPRODUCE claim
OUTPUT_HEAD, OUTPUT_TAIL = 1000, 3000

_ENV_PATTERNS = [
    r"command not found", r"No such file or directory: '?(?:npx|node|python3?|go|mvn|gradle|cargo|bundle|dotnet)\b",
    r"Cannot find module", r"ModuleNotFoundError", r"No module named", r"ENOENT.*(?:node_modules|package\.json)",
    r"Cannot connect to the Docker daemon", r"npm (?:ERR!|error) (?:code )?(?:E401|E403|ENOTFOUND|ETIMEDOUT)",
    r"could not find `?Cargo\.toml", r"permission denied.*node_modules", r"error: no such command",
]
# Output suggesting that no test ran. Decisive only when nothing in the output shows a test that did run: a multi-package
# `go test ./...` prints "[no test files]" next to real "ok" lines, and a passing pytest log can mention "no tests ran".
_ZERO_TESTS = [
    r"collected 0 items", r"no tests ran", r"No tests found", r"\[no test files\]", r"\[no tests to run\]", r"Tests run: 0\b",
    r"\bRan 0 tests\b", r"No tests were run", r"Test Suites: 0 total", r"running 0 tests", r"No test files found",
    r"(?<![\d.])0 passing", r"Tests:\s+(?:\d+ (?:skipped|todo),\s*)+\d+ total",
]
_BUILD_PATTERNS = [
    r"error TS\d+", r"SyntaxError", r"COMPILATION ERROR", r"error\[E\d+\]", r"cannot find symbol",
    r"compilation failed", r"^# .*\n.*\.go:\d+:\d+:", r"CS\d{4}: ",
]
# Evidence that at least one test ran and passed (a Go package that really ran prints a bare "ok <pkg> <time>").
_POSITIVE_PASS = re.compile(
    r"(?<![\d.])[1-9]\d*\s+(?:passed|passing)|test result: ok\. [1-9]|Tests run: [1-9]|^ok\s+\S+\s+[\d.]+s(?:\s+\(cached\))?\s*$", re.I | re.M)
_FAIL_LINE = re.compile(r"(AssertionError|Error|Exception|FAIL|panic|expected|✕|×|Timeout)", re.I)


@dataclass
class Iteration:
    index: int
    phase: str
    exit_code: int
    duration_s: float
    timed_out: bool
    output_tail: str
    failure_class: str | None  # None=pass, 'test' | 'timeout' | 'build' | 'env'
    fingerprint: str | None
    note: str = ""


@dataclass
class RunSet:
    iterations: list[Iteration] = field(default_factory=list)
    stop_reason: str = ""
    argv_full: list[str] = field(default_factory=list)
    argv_isolated: list[str] | None = None
    parallel: int = 1


def classify_failure(output: str, exit_code: int, timed_out: bool = False) -> tuple[str | None, str]:
    """Return (failure_class, note). ``None`` means a genuine pass."""
    if timed_out:
        return "timeout", "run exceeded the timeout"
    zero = any(re.search(p, output, re.I) for p in _ZERO_TESTS) and not _POSITIVE_PASS.search(output)
    if exit_code == 0:
        if zero:
            return "env", "exit 0 but zero tests executed"
        return None, ""
    if exit_code in (126, 127) or any(re.search(p, output, re.I | re.M) for p in _ENV_PATTERNS):
        return "env", "runner or dependency unavailable"
    if zero and not _FAIL_LINE.search(output):
        return "env", "zero tests executed"
    if any(re.search(p, output, re.M) for p in _BUILD_PATTERNS):
        return "build", "compile/syntax error"
    return "test", ""


def fingerprint(output: str) -> str | None:
    """Normalized first failure line, so identical failures cluster and different ones separate."""
    for line in output.splitlines():
        if _FAIL_LINE.search(line) and line.strip():
            s = line.strip().lower()
            s = re.sub(r"0x[0-9a-f]+|[0-9a-f]{8,}", "<hex>", s)
            s = re.sub(r"(?:/[\w.@-]+){2,}", "<path>", s)
            s = re.sub(r"\d+(?:\.\d+)?", "N", s)
            s = re.sub(r"(['\"`]).*?\1", "<s>", s)
            return s[:160]
    return None


def extract_failing_title(output: str) -> str | None:
    """First failing test name found in runner output (pytest, jest, go, rspec, junit). Mocha titles are not extracted: its numbered listing is ambiguous."""
    patterns = [
        r"FAILED\s+[\w./-]+\.py::([\w:\[\]. -]+?)(?:\s+-|\s*$)", r"●\s+.+?\s+›\s+(.+)$",
        r"--- FAIL: (Test\w+)", r"rspec\s+\S+_spec\.rb:\d+\s+#\s+(.+)$", r"\b\w+\.(test\w+|\w+Test)\s+FAILED",
    ]
    for p in patterns:
        m = re.search(p, output, re.M)
        if m:
            return m.group(1).strip()
    return None


def _clip(text: str) -> str:
    if len(text) <= OUTPUT_HEAD + OUTPUT_TAIL:
        return text
    return f"{text[:OUTPUT_HEAD]}\n…[{len(text) - OUTPUT_HEAD - OUTPUT_TAIL} chars omitted]…\n{text[-OUTPUT_TAIL:]}"


def _read_clipped(f, size: int, max_bytes: int) -> str:
    """Head and tail of a captured output file, so a runaway test cannot exhaust memory."""
    if size <= max_bytes:
        f.seek(0)
        return f.read().decode("utf-8", "replace")
    head = max_bytes // 4
    f.seek(0)
    first = f.read(head).decode("utf-8", "replace")
    f.seek(size - (max_bytes - head))
    last = f.read().decode("utf-8", "replace")
    return f"{first}\n…[{size - max_bytes} bytes omitted]…\n{last}"


def run_capture(argv: list[str], cwd: str, env: dict[str, str] | None, timeout: float, max_bytes: int = 2_000_000) -> tuple[str, int, bool]:
    """Run a command and return (combined output, exit code, timed_out). Never raises.

    The command runs in its own process group with the platform's credentials removed from its environment (it is
    code from the repository under test). The group is always killed afterwards, so background servers a test
    started cannot hold ports into the next run. Output goes to a temporary file and only its head and tail are
    read back (``max_bytes``).
    """
    if not argv:
        return "empty command", 126, False
    if not os.path.isdir(cwd):
        return f"working directory not found: {cwd}", 126, False
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=scrubbed_environ(env), stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        except FileNotFoundError:
            return f"command not found: {argv[0]}", 127, False
        except OSError as e:
            return f"cannot execute {argv[0]}: {e}", 126, False
        timed_out = False
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out, code = True, -1
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if timed_out:
            proc.wait()
        return _read_clipped(out, out.seek(0, os.SEEK_END), max_bytes), code, timed_out


def run_once(argv: list[str], cwd: str, env: dict[str, str] | None, timeout: int, index: int = 0, phase: str = "full") -> Iteration:
    """Run one iteration and classify it."""
    start = time.monotonic()
    out, code, timed_out = run_capture(argv, cwd, {**(env or {}), "FLAKY_RUN_INDEX": str(index)}, timeout)
    cls, note = classify_failure(out, code, timed_out)
    return Iteration(index, phase, code, round(time.monotonic() - start, 3), timed_out, _clip(out), cls,
                     fingerprint(out) if cls else None, note)


def _counts(iters: list[Iteration]) -> tuple[int, int, int]:
    passes = sum(1 for i in iters if i.failure_class is None)
    fails = sum(1 for i in iters if i.failure_class in ("test", "timeout", "build"))
    env = sum(1 for i in iters if i.failure_class == "env")
    return passes, fails, env


def stop_reason(iters: list[Iteration], threshold: int = DETERMINISTIC_FAIL_THRESHOLD) -> str:
    """Early-stop decision. Empty string means keep going."""
    if iters and iters[0].failure_class == "env":
        return "environment_failure"
    passes, fails, _ = _counts(iters)
    if passes and fails:
        return "mixed_pass_fail"
    tail = [i for i in iters if i.failure_class != "env"][-threshold:]
    if not passes and len(tail) >= threshold and all(i.failure_class for i in tail) and len({i.fingerprint for i in tail}) == 1:
        return "consecutive_failures"
    return ""


def smart_rerun(full_argv: list[str], *, cwd: str, runs: int = DEFAULT_RUNS,
                isolate: Callable[[str | None], list[str] | None] | None = None,
                full_ratio: float = FULL_RUN_RATIO, early_stop: bool = True, parallel: int = 1,
                env: dict[str, str] | None = None, timeout: int = 900) -> RunSet:
    """Execute the two-phase rerun and return every iteration plus why it stopped."""
    rs = RunSet(argv_full=full_argv, parallel=max(1, parallel))
    n_full = min(runs, max(1, math.ceil(runs * full_ratio))) if isolate else runs
    idx = 0

    def one(argv: list[str], phase: str, i: int) -> Iteration:
        return run_once(argv, cwd, env, timeout, index=i, phase=phase)

    for _ in range(n_full):
        rs.iterations.append(one(full_argv, "full", idx))
        idx += 1
        if rs.iterations[-1].failure_class == "env" and idx == 1:
            rs.stop_reason = "environment_failure"
            return rs
        if early_stop and not isolate and (reason := stop_reason(rs.iterations)):
            rs.stop_reason = reason
            return rs
    if early_stop and (reason := stop_reason(rs.iterations)):
        rs.stop_reason = reason
        return rs

    argv2, phase2 = full_argv, "full"
    if isolate:
        first_fail = next((i for i in rs.iterations if i.failure_class in ("test", "timeout")), None)
        isolated = isolate(extract_failing_title(first_fail.output_tail) if first_fail else None) if first_fail else None
        if isolated:
            argv2, phase2, rs.argv_isolated = isolated, "isolated", isolated
    remaining = runs - n_full
    while remaining > 0:
        batch = min(max(1, parallel), remaining)
        if batch == 1:
            results = [one(argv2, phase2, idx)]
        else:
            with ThreadPoolExecutor(max_workers=batch) as pool:
                results = list(pool.map(lambda k: one(argv2, phase2, idx + k), range(batch)))
        rs.iterations.extend(results)
        idx += batch
        remaining -= batch
        if early_stop and (reason := stop_reason(rs.iterations)):
            rs.stop_reason = reason
            return rs
    rs.stop_reason = "completed"
    return rs


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k failures in n runs."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def verdict(rs: RunSet, requested_runs: int) -> dict:
    """Summarize a RunSet into a verdict plus statistics.

    Early stopping on a proven pass+fail mix biases any rate (optional stopping), so no rate or interval is reported
    then. All-failing runs are only DETERMINISTIC_FAIL when enough runs completed; fewer is INCONCLUSIVE.
    """
    iters = rs.iterations
    passes, fails, env = _counts(iters)
    completed = passes + fails
    stopped_on_mix = rs.stop_reason == "mixed_pass_fail"
    if completed == 0:
        v = "ENV_BLOCKED"
    elif fails == 0:
        v = "CANNOT_REPRODUCE"
    elif passes == 0:
        v = "DETERMINISTIC_FAIL" if completed >= DETERMINISTIC_FAIL_THRESHOLD else "INCONCLUSIVE"
    else:
        v = "FLAKY"
    low, high = wilson(fails, completed)
    prints = Counter(i.fingerprint for i in iters if i.failure_class and i.failure_class != "env" and i.fingerprint)
    first_fail = next((i.index for i in iters if i.failure_class in ("test", "timeout", "build")), None)
    return {
        "verdict": v, "requested_runs": requested_runs, "executed": len(iters), "completed": completed,
        "passes": passes, "failures": fails, "env_failures": env, "parallel": rs.parallel,
        "failure_rate": None if (not completed or stopped_on_mix) else round(fails / completed, 4),
        "wilson95": None if (not completed or stopped_on_mix) else [round(low, 4), round(high, 4)], "stop_reason": rs.stop_reason,
        "first_failure_index": first_fail, "failure_classes": dict(Counter(i.failure_class for i in iters if i.failure_class)),
        "failure_signatures": [{"fingerprint": f, "count": c} for f, c in prints.most_common(5)],
        "phases": dict(Counter(i.phase for i in iters)),
        "total_duration_s": round(sum(i.duration_s for i in iters), 2),
        "build_failure": bool(fails and all(i.failure_class == "build" for i in iters if i.failure_class not in (None, "env"))),
        "statement": _statement(v, fails, completed, high, stopped_on_mix, rs.parallel),
    }


def _statement(v: str, fails: int, completed: int, high: float, stopped_on_mix: bool = False, parallel: int = 1) -> str:
    if v == "CANNOT_REPRODUCE":
        base = f"0/{completed} failures (95% upper bound {high:.1%}); not proof the test is stable"
        if completed < MIN_RUNS_FOR_NEGATIVE:
            base += f"; only {completed} completed runs (minimum {MIN_RUNS_FOR_NEGATIVE}), inconclusive"
    elif v == "ENV_BLOCKED":
        return "no run executed tests; fix the environment before drawing any conclusion"
    elif v == "INCONCLUSIVE":
        base = f"{fails}/{completed} runs failed, too few completed runs (minimum {DETERMINISTIC_FAIL_THRESHOLD}) to call it deterministic; rerun with more runs"
    elif stopped_on_mix:
        base = f"{fails}/{completed} runs failed before the loop stopped early: flakiness is proven, the failure rate is not estimated"
    else:
        base = f"{fails}/{completed} runs failed ({fails / completed:.1%}, 95% CI {_ci(fails, completed)})"
    if parallel > 1:
        base += f"; ran {parallel}-way parallel, so shared ports or files can cause failures of their own"
    return base


def _ci(fails: int, n: int) -> str:
    lo, hi = wilson(fails, n)
    return f"{lo:.1%}-{hi:.1%}"


def _isolate_from_template(template: list[str]) -> Callable[[str | None], list[str] | None]:
    def build(title: str | None) -> list[str] | None:
        if not title:
            return None
        return [t.replace("{test}", title) for t in template]
    return build


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cwd", default=".")
    p.add_argument("--cmd", required=True, help="command for the full file/suite (split like a shell, run without a shell)")
    p.add_argument("--isolate-cmd", default="", help="command template with {test}; used after phase 1 sees a failure")
    p.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--env", action="append", default=[], metavar="K=V")
    p.add_argument("--full-ratio", type=float, default=FULL_RUN_RATIO)
    p.add_argument("--no-early-stop", action="store_true")
    p.add_argument("--out", default="runs.json")
    args = p.parse_args(argv)
    if args.runs < 1:
        print("--runs must be >= 1", file=sys.stderr)
        return 2
    bad = [e for e in args.env if "=" not in e]
    cmd = shlex.split(args.cmd)
    if bad or not cmd:
        print(f"--env expects KEY=VALUE (got {bad}) and --cmd must not be empty" if bad else "--cmd must not be empty", file=sys.stderr)
        return 2
    env = dict(e.split("=", 1) for e in args.env)
    isolate = _isolate_from_template(shlex.split(args.isolate_cmd)) if args.isolate_cmd else None
    rs = smart_rerun(cmd, cwd=args.cwd, runs=args.runs, isolate=isolate, full_ratio=args.full_ratio,
                     early_stop=not args.no_early_stop, parallel=args.parallel, env=env, timeout=args.timeout)
    summary = verdict(rs, args.runs)
    Path(args.out).write_text(json.dumps({"summary": summary, "argv_full": rs.argv_full, "argv_isolated": rs.argv_isolated,
                                          "iterations": [asdict(i) for i in rs.iterations]}, indent=2), encoding="utf-8")
    print(f"[flaky] {summary['verdict']}: {summary['statement']} (stop={summary['stop_reason']}) -> {args.out}", flush=True)
    print(f"::add-task-context FLAKY_VERDICT::{summary['verdict']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
