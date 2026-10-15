"""Find which earlier test files make a target test fail (order-dependent / shared-state flakes).

Runner-agnostic delta debugging (ddmin): the command template receives the candidate files plus the
target (target last) and a failure *pattern* decides whether the target failed. A pattern is required
because earlier candidates may fail for their own reasons, which would make exit codes misleading.

Usage:
    python3 -m scripts.flaky.bisect_polluter --cmd "pytest -p no:cacheprovider {files}" \
        --target tests/test_cart.py --candidates tests/test_a.py tests/test_b.py \
        --fail-pattern "test_cart.py.*FAILED|FAILED.*test_cart" [--repeat 2] [--max-runs 60] [--cwd DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from typing import Callable

from scripts.common.exit_codes import EXIT_BLOCKED, EXIT_USAGE
from scripts.flaky.rerun import classify_failure, run_capture


class EnvBlocked(RuntimeError):
    """The runner itself could not execute (missing binary/deps), so no conclusion is possible."""


def ddmin(candidates: list[str], fails: Callable[[list[str]], bool], max_runs: int = 60) -> tuple[list[str], int, bool]:
    """Minimal failing subset (order preserved). Returns (subset, predicate_calls, exhausted)."""
    calls = 0

    def test(subset: list[str]) -> bool:
        nonlocal calls
        calls += 1
        return fails(subset)

    current, n = list(candidates), 2
    while len(current) >= 2:
        if calls >= max_runs:
            return current, calls, True
        size = max(1, len(current) // n)
        chunks = [current[i:i + size] for i in range(0, len(current), size)]
        reduced = False
        for chunk in chunks:  # try each chunk alone
            if calls >= max_runs:
                return current, calls, True
            if test(chunk):
                current, n, reduced = chunk, 2, True
                break
        if not reduced:
            for chunk in chunks:  # try each complement
                comp = [c for c in current if c not in chunk]
                if comp and calls < max_runs and test(comp):
                    current, n, reduced = comp, max(n - 1, 2), True
                    break
        if not reduced:
            if n >= len(current):
                break
            n = min(len(current), n * 2)
    return current, calls, False


def find_polluters(candidates: list[str], fails: Callable[[list[str]], bool], max_runs: int = 60) -> dict:
    """``fails(files_before_target)`` -> True when the target fails after running those files first."""
    if fails([]):
        return {"status": "fails_alone", "polluters": [], "detail": "target fails without any other file: not order-dependent"}
    if not candidates or not fails(candidates):
        return {"status": "not_reproduced", "polluters": [],
                "detail": "target passes after all candidates: no order dependence among them (or it is intermittent)"}
    subset, calls, exhausted = ddmin(candidates, fails, max_runs)
    return {"status": "budget_exhausted" if exhausted else "found", "polluters": subset, "predicate_calls": calls + 2}


def make_predicate(cmd_template: list[str], target: str, fail_pattern: str, cwd: str, repeat: int = 1,
                   timeout: int = 900) -> Callable[[list[str]], bool]:
    rx = re.compile(fail_pattern)

    def run(files: list[str]) -> bool:
        files_arg = [*files, target]
        argv = [a for t in cmd_template for a in (files_arg if t == "{files}" else [t])]
        for _ in range(repeat):
            out, code, timed_out = run_capture(argv, cwd, None, timeout, max_bytes=4_000_000)
            if timed_out:
                # A hang says nothing certain about the target; guessing would blame the wrong file.
                raise EnvBlocked(f"a run timed out after {timeout}s, so order dependence cannot be judged")
            if code in (126, 127):
                raise EnvBlocked(out.strip() or f"command not found: {argv[0]}")
            if rx.search(out):
                return True
            if classify_failure(out, code)[0] == "env":
                last = out.strip().splitlines()[-1][:200] if out.strip() else "no output"
                raise EnvBlocked(f"runner could not execute tests: {last}")
        return False
    return run


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cmd", required=True, help="command template containing the token {files}")
    p.add_argument("--target", required=True)
    p.add_argument("--candidates", nargs="+", required=True)
    p.add_argument("--fail-pattern", required=True, help="regex that matches output only when the target failed")
    p.add_argument("--cwd", default=".")
    p.add_argument("--repeat", type=int, default=1, help="runs per predicate call (any failure counts)")
    p.add_argument("--max-runs", type=int, default=60)
    args = p.parse_args(argv)
    template = shlex.split(args.cmd)
    if "{files}" not in template:
        print("--cmd must contain a {files} token", file=sys.stderr)
        return EXIT_USAGE
    cands = [c for c in args.candidates if c != args.target]
    try:
        result = find_polluters(cands, make_predicate(template, args.target, args.fail_pattern, args.cwd, args.repeat), args.max_runs)
    except EnvBlocked as e:
        result = {"status": "env_blocked", "polluters": [], "detail": str(e)}
    print(json.dumps(result, indent=2))
    print(f"::add-task-context POLLUTER_STATUS::{result['status']}", flush=True)
    return EXIT_BLOCKED if result["status"] == "env_blocked" else 0


if __name__ == "__main__":
    sys.exit(main())
