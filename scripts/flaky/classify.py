"""Diagnose likely flakiness causes from failing output (runtime) and test source (static).

Language-agnostic regex signatures. Each finding carries a reason, a concrete recommendation and
evidence (a matched output line, or ``file:line`` plus the source line). Static findings win over
runtime findings of the same kind because they point at code. Findings are *hypotheses*: the skill
must still test one at a time and never claim a root cause from a signature alone.

Usage:
    python3 -m scripts.flaky.classify --runs runs.json --source tests/a.test.js [--source ...] --out findings.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

# id -> (severity, reason, recommendation)
_META = {
    "fixed_sleep": ("high", "A fixed sleep/timeout guesses how long something takes, so it fails when the machine is slower.",
                    "Wait for the condition (poll with a deadline, await the event) instead of sleeping."),
    "timeout": ("high", "An operation exceeded its time budget.",
                "Find what the test waits for; wait on that condition, and only raise a limit with evidence it is legitimately slow."),
    "port_conflict": ("high", "The test binds or connects to a fixed port that another process or run may hold.",
                      "Bind to port 0 / an OS-assigned free port and pass the port to the client."),
    "fixed_port": ("high", "Hard-coded port numbers collide when tests run in parallel or ports are in use.",
                   "Bind to port 0 / an OS-assigned free port and pass the port to the client."),
    "data_race": ("high", "A data race, deadlock or concurrent modification was reported.",
                  "Synchronize shared state; run the race detector (e.g. go test -race) and fix the reported access."),
    "wall_clock": ("medium", "The test reads the real clock, so results depend on when and how fast it runs.",
                   "Inject a clock or use fake timers; assert on injected time, not Date.now()/now()."),
    "time_bomb": ("medium", "A hard-coded date at or after this year will eventually be in the past and break the test.",
                  "Compute dates relative to the (fake) current time instead of literals."),
    "clock_expiry": ("medium", "Output mentions an expired/not-yet-valid time window (certificate, token, fixture).",
                     "Regenerate the fixture at test time or freeze the clock."),
    "unseeded_random": ("medium", "Unseeded randomness makes inputs differ between runs.",
                        "Seed the generator (log the seed) or use fixed inputs for assertions."),
    "shared_state": ("medium", "Module-level mutable state or environment changes leak between tests.",
                     "Create state per test and restore env/globals in teardown."),
    "tmp_path_shared": ("medium", "A fixed temp path is shared across tests and runs.",
                        "Use a per-test temp directory (mkdtemp, t.TempDir(), tmp_path)."),
    "network_dependency": ("medium", "The test depends on a real external network endpoint.",
                           "Stub or record the dependency (httptest, nock, WireMock, responses)."),
    "network_error": ("medium", "Connection-level errors appeared during the run.",
                      "Check whether a dependency starts late or is shared; wait for readiness and isolate it per test."),
    "filesystem_state": ("medium", "Filesystem errors (missing/exists/permission) suggest leftover state.",
                         "Create and clean up files per test in an isolated directory."),
    "async_race": ("medium", "An undefined/null access or unhandled rejection suggests an operation was not awaited.",
                   "Await every async step and assert only after the awaited condition is met."),
    "resource_pressure": ("medium", "The process ran out of memory or file handles.",
                          "Reduce parallelism or leaks; check whether the failure correlates with load."),
    "concurrency_in_test": ("low", "The test spawns threads/goroutines/parallel tasks.",
                            "Join or wait on every worker explicitly before asserting."),
    "unordered_iteration": ("low", "Iterating a map/set/keys has no guaranteed order.",
                            "Sort before comparing or compare as sets."),
    "timezone_locale": ("low", "Locale/timezone-dependent formatting.", "Pin TZ and locale in the test environment."),
    "retry_masking": ("info", "Retries or a flaky marker already exist and may hide the real failure rate.",
                      "Measure the failure rate with retries disabled."),
    "order_dependence": ("high", "Fails in full-file runs but never when the test runs alone: another test leaves state behind.",
                         "Bisect the preceding tests (bisect_polluter) and isolate the shared state."),
}

_STATIC: list[tuple[str, re.Pattern[str]]] = [
    ("fixed_sleep", re.compile(r"\b(?:time\.sleep|Thread\.sleep|usleep|asyncio\.sleep|time\.Sleep|thread::sleep)\s*\(|"
                               r"\bsetTimeout\s*\(|\bwaitForTimeout\s*\(|\bsleep\s+\d")),
    ("wall_clock", re.compile(r"\b(?:Date\.now\(\)|new Date\(\)|time\.time\(\)|datetime\.(?:now|utcnow|today)\(\)|"
                              r"System\.currentTimeMillis|Instant\.now\(\)|LocalDate(?:Time)?\.now\(|time\.Now\(\)|DateTime\.(?:Utc)?Now)")),
    ("fixed_port", re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d{4,5}\b|\.listen\(\s*\d{4,5}|\bport\s*[:=]\s*\d{4,5}\b|"
                              r"ListenAndServe\(\s*\":\d{4,5}\"")),
    ("unseeded_random", re.compile(r"\bMath\.random\(|\brandom\.(?:random|randint|choice|shuffle|sample)\(|"
                                   r"\brand\.(?:Int|Intn|Float64)\(|\bnew Random\(\)|\buuid4\(")),
    ("shared_state", re.compile(r"^(?:let|var)\s+\w+\s*=\s*(?:\[\]|\{\}|new (?:Map|Set)\()|^\w+\s*=\s*(?:\[\]|\{\}|dict\(\)|list\(\))\s*$|"
                                r"\bglobal\s+\w+|(?:process\.env\.\w+|os\.environ\[['\"]\w+['\"]\])\s*=[^=]", re.M)),
    ("tmp_path_shared", re.compile(r"['\"]/tmp/[\w.-]+['\"/]")),
    ("network_dependency", re.compile(r"https?://(?!localhost|127\.|0\.0\.0\.0|\[::1\]|example\.(?:com|org|net))[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}")),
    ("unordered_iteration", re.compile(r"\blist\(\s*set\(|Object\.(?:keys|values|entries)\(")),
    ("concurrency_in_test", re.compile(r"\bthreading\.Thread\b|\bThread\(|\bgo func\(|ExecutorService|ThreadPool|\bPromise\.all\b")),
    ("timezone_locale", re.compile(r"toLocale(?:Date|Time)?String|strftime|DateTimeFormatter\.ofPattern|time\.Local\b")),
    ("retry_masking", re.compile(r"jest\.retryTimes|\bretries\s*[:=]|@flaky|pytest\.mark\.flaky|\bflaky\(|reruns?\s*[:=]")),
]
_DATE_LITERAL = re.compile(r"\b(20[2-9]\d)-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b")
_SEED_HINT = re.compile(r"\bseed\s*\(|\bRandom\(\s*\d|--seed|random_state|\bseed\s*[:=]", re.I)
_COMMENT = re.compile(r"^\s*(?://|#|\*|/\*|--)")

_RUNTIME: list[tuple[str, re.Pattern[str]]] = [
    ("timeout", re.compile(r"timed? ?out|exceeded timeout|deadline exceeded|ETIMEDOUT|Timeout of \d+", re.I)),
    ("port_conflict", re.compile(r"EADDRINUSE|address already in use|bind: address", re.I)),
    ("data_race", re.compile(r"DATA RACE|ConcurrentModificationException|deadlock|race condition", re.I)),
    ("async_race", re.compile(r"Cannot read propert(?:y|ies) of (?:undefined|null)|is not a function|UnhandledPromiseRejection|"
                              r"unhandled promise|NullPointerException|nil pointer dereference", re.I)),
    ("network_error", re.compile(r"ECONNREFUSED|ECONNRESET|ENOTFOUND|connection refused|socket hang up|"
                                 r"Name or service not known|Temporary failure in name resolution", re.I)),
    ("filesystem_state", re.compile(r"\bENOENT\b|\bEACCES\b|\bEEXIST\b|\bENOTEMPTY\b|File exists|Text file busy", re.I)),
    ("resource_pressure", re.compile(r"out of memory|heap out of memory|\bENOMEM\b|too many open files|\bEMFILE\b|"
                                     r"cannot allocate memory|\bKilled\b", re.I)),
    ("clock_expiry", re.compile(r"certificate has expired|not yet valid|\bexpired\b|\bNotBefore\b|clock skew", re.I)),
]


def scan_output(text: str) -> list[dict]:
    """Runtime signatures in failing output. Evidence is the first matching line."""
    findings = []
    for fid, rx in _RUNTIME:
        for line in (text or "").splitlines():
            if rx.search(line):
                sev, reason, rec = _META[fid]
                findings.append({"id": fid, "severity": sev, "source": "runtime", "reason": reason,
                                 "recommendation": rec, "evidence": line.strip()[:240]})
                break
    return findings


def scan_source(path: str, text: str, today: datetime.date | None = None, per_id: int = 5) -> list[dict]:
    """Static signatures with file:line. Comment-only lines are ignored."""
    today = today or datetime.date.today()
    seeded = bool(_SEED_HINT.search(text))
    found: dict[str, list[dict]] = {}
    lines = text.splitlines()

    def add(fid: str, n: int, line: str) -> None:
        sev, reason, rec = _META[fid]
        bucket = found.setdefault(fid, [])
        if len(bucket) < per_id:
            bucket.append({"id": fid, "severity": sev, "source": "static", "reason": reason, "recommendation": rec,
                           "file": path, "line": n, "snippet": line.strip()[:200], "evidence": f"{path}:{n}"})

    for n, line in enumerate(lines, 1):
        if not line.strip() or _COMMENT.match(line):
            continue
        for fid, rx in _STATIC:
            if fid == "shared_state":
                continue  # multi-line anchored pattern handled below
            if rx.search(line):
                if fid == "unseeded_random" and seeded:
                    continue
                add(fid, n, line)
        for m in _DATE_LITERAL.finditer(line):
            if int(m.group(1)) >= today.year:
                add("time_bomb", n, line)
    shared_rx = dict(_STATIC)["shared_state"]
    for m in shared_rx.finditer(text):
        n = text.count("\n", 0, m.start()) + 1
        if not _COMMENT.match(lines[n - 1]):
            add("shared_state", n, lines[n - 1])
    return [f for fs in found.values() for f in fs]


def phase_findings(iterations: list[dict]) -> list[dict]:
    """Order/shared-state dependence: failures only in full-file runs, isolated runs all pass."""
    fails = lambda phase: sum(1 for i in iterations if i["phase"] == phase and i["failure_class"] in ("test", "timeout"))
    passes = lambda phase: sum(1 for i in iterations if i["phase"] == phase and i["failure_class"] is None)
    if fails("full") and not fails("isolated") and passes("isolated") >= 3:
        sev, reason, rec = _META["order_dependence"]
        return [{"id": "order_dependence", "severity": sev, "source": "runtime", "reason": reason, "recommendation": rec,
                 "evidence": f"full-file: {fails('full')} failing run(s); isolated: 0 of {passes('isolated')} failed"}]
    return []


def diagnose(runs: dict, sources: dict[str, str], today: datetime.date | None = None) -> list[dict]:
    """Merge runtime + static + phase findings; static replaces runtime of the same id; sort by severity."""
    iters = runs.get("iterations", [])
    failing_text = "\n".join(i["output_tail"] for i in iters if i["failure_class"] in ("test", "timeout"))
    runtime = scan_output(failing_text) + phase_findings(iters)
    static = [f for p, t in sources.items() for f in scan_source(p, t, today)]
    static_ids = {f["id"] for f in static}
    # runtime signatures with a different name for the same cause still point at static hits
    aliases = {"timeout": "fixed_sleep", "port_conflict": "fixed_port", "network_error": "network_dependency"}
    for f in static:
        match = next((r for r in runtime if r["id"] == f["id"] or aliases.get(r["id"]) == f["id"]), None)
        if match:
            f["runtime_evidence"] = match["evidence"]
    kept = [r for r in runtime if r["id"] not in static_ids and aliases.get(r["id"]) not in static_ids]
    merged = static + kept
    merged.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["source"] != "static", f["id"], f.get("line", 0)))
    return merged


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs", required=True)
    p.add_argument("--source", action="append", default=[])
    p.add_argument("--out", default="findings.json")
    args = p.parse_args(argv)
    runs = json.loads(Path(args.runs).read_text(encoding="utf-8"))
    sources = {s: Path(s).read_text(encoding="utf-8", errors="replace") for s in args.source if Path(s).is_file()}
    findings = diagnose(runs, sources)
    Path(args.out).write_text(json.dumps({"findings": findings, "sources_scanned": list(sources)}, indent=2), encoding="utf-8")
    print(f"[flaky] {len(findings)} signature(s) -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
