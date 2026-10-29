"""Compare baseline and candidate QA runs: per-case delta, new log errors, latency regressions.

Per-case delta (matched by case id):
    pass/pass -> BOTH_PASS        pass/fail -> REGRESSION      fail/pass -> FIXED
    fail/fail -> PRE_EXISTING     only candidate -> NEW        only baseline -> REMOVED
    blocked or infra on either side -> INFRA (never a regression)
    not-run / inconclusive on either side -> INCONCLUSIVE
    baseline fail whose message is a timeout / connection / 502-504 error -> INCONCLUSIVE (not FIXED)

Run status precedence: BLOCKED (a side executed nothing) > REGRESSION > NEW_FAILURES (a case only the candidate
has, failing) > INCOMPLETE (infra, blocked, inconclusive or removed cases) > PRE_EXISTING (fails on both sides)
> FIXED > CLEAN. CLEAN is therefore impossible when a side executed nothing or any case could not be compared.

Usage:
    python3 -m scripts.qa.delta --baseline base.jsonl --candidate cand.jsonl \
        [--baseline-log b.log --candidate-log c.log] [--perf-baseline b.json --perf-candidate c.json] [--noise noise.txt] [--plan plan.json] --out delta.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from scripts.qa.model import _NO_RESULT, case_outcomes, load_jsonl, summarize

PERF_REGRESSION_RATIO = 1.20
_LEVEL_RX = re.compile(r"\b(ERROR|FATAL|CRITICAL|PANIC|EXCEPTION|TRACEBACK|WARN(?:ING)?)\b", re.I)
# Known-harmless access-log lines only; level words like debug/trace are handled by severity detection instead.
_NOISE_DEFAULT = [r"\b(?:GET|HEAD)\s+/\S*(?:health|ready|live|ping)\S*\s", r"favicon\.ico"]
_LOW_LEVEL = re.compile(r"^\W*(?:\S+\s+)?\[?(?:DEBUG|TRACE|INFO)\]?\b", re.I)  # "INFO ...", "2026-01-01T00:00:00Z DEBUG ..."
_ERROR_LEVELS = {"error", "err", "fatal", "critical", "crit", "panic", "severe", "emerg", "alert"}
_INCREASE_FACTOR, _INCREASE_MIN = 3, 5


# A baseline failure whose message names a connectivity or gateway problem says nothing about the product: do not
# let it turn a candidate pass into FIXED. (The candidate side keeps such failures: a timeout there is a regression.)
_TRANSIENT = re.compile(r"timed? ?out|timeout|ECONNREFUSED|ECONNRESET|connection (?:refused|reset)|\b50[234]\b|bad gateway|service unavailable", re.I)


def classify_case(base: dict | None, cand: dict | None) -> str:
    if base is None and cand is None:
        raise ValueError("both sides missing")
    if base is None:
        return "NEW"
    if cand is None:
        return "REMOVED"
    b, c = base["outcome"], cand["outcome"]
    if "blocked" in (b, c) or base.get("infra") or cand.get("infra"):
        return "INFRA"
    if b in ("not-run", "inconclusive") or c in ("not-run", "inconclusive"):
        return "INCONCLUSIVE"
    if b == "fail" and any(_TRANSIENT.search(m) for m in base.get("messages", [])):
        return "INCONCLUSIVE"
    if b == "pass" and c == "pass":
        return "BOTH_PASS"
    if b == "pass" and c == "fail":
        return "REGRESSION"
    if b == "fail" and c == "pass":
        return "FIXED"
    return "PRE_EXISTING"


_CODE_TABLE = str.maketrans("0123456789", "abcdefghij")


def normalize_log_line(line: str) -> str:
    """Strip volatile tokens so the same error clusters across runs."""
    s = line.strip().lower()
    s = re.sub(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?z?", "<ts>", s)
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<uuid>", s)
    s = re.sub(r"\b[0-9a-hjkmnp-tv-z]{26}\b", "<ulid>", s)
    s = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", "<ip>", s)
    s = re.sub(r"\b[0-9a-f]{12,}\b", "<hex>", s)
    # Keep HTTP-ish status codes distinguishable (status 404 vs 500), then drop every other number.
    s = re.sub(r"(?i)\b(status|code|http)([\s:=]*)([1-5]\d\d)\b", lambda m: f"{m.group(1)}{m.group(2)}" + m.group(3).translate(_CODE_TABLE), s)
    s = re.sub(r"\d+", "N", s)  # also catches digits glued to units, e.g. 31ms
    return s[:200]


def log_errors(text: str, noise: list[str] | None = None) -> dict[str, dict]:
    """Cluster error/warn lines by normalized form. A JSON ``level`` field outranks keywords in the message."""
    noise_rx = [re.compile(p, re.I) for p in (_NOISE_DEFAULT + (noise or []))]
    clusters: dict[str, dict] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        message, severity = line, None
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                level = str(obj.get("level", obj.get("severity", ""))).lower()
                message = str(obj.get("msg", obj.get("message", line)))
                severity = "error" if level in _ERROR_LEVELS else ("warn" if level.startswith("warn") else None)
            except (json.JSONDecodeError, AttributeError):
                obj = None
        if severity is None and not line.startswith("{") and not _LOW_LEVEL.match(line):
            m = _LEVEL_RX.search(line)
            severity = None if not m else ("warn" if m.group(1).upper().startswith("WARN") else "error")
        if severity is None or any(rx.search(message) for rx in noise_rx):
            continue
        c = clusters.setdefault(normalize_log_line(message if line.startswith("{") else line),
                                {"severity": severity, "count": 0, "sample": line[:240]})
        c["count"] += 1
    return clusters


def error_changes(base_text: str, cand_text: str, noise: list[str] | None = None) -> list[dict]:
    """Candidate error clusters that are new, or that grew by at least 3x (and 5 occurrences) over the baseline."""
    base = log_errors(base_text, noise)
    out = []
    for key, c in log_errors(cand_text, noise).items():
        before = base.get(key, {}).get("count", 0)
        if key not in base:
            out.append({"normalized": key, "kind": "new", "baseline_count": 0, **c})
        elif c["count"] >= max(_INCREASE_MIN, _INCREASE_FACTOR * before):
            out.append({"normalized": key, "kind": "increased", "baseline_count": before, **c})
    return out


def perf_regressions(base: dict[str, float], cand: dict[str, float], ratio: float = PERF_REGRESSION_RATIO) -> list[dict]:
    """p50 latency per endpoint; flags candidates slower than baseline by more than ``ratio``."""
    out = []
    for name, c50 in cand.items():
        b50 = base.get(name)
        if b50 and b50 > 0 and c50 / b50 > ratio:
            out.append({"endpoint": name, "baseline_p50_ms": b50, "candidate_p50_ms": c50, "ratio": round(c50 / b50, 2)})
    return out


def compare(base_records: list[dict], cand_records: list[dict], plan_ids: set[str] | None = None) -> dict:
    """Per-case delta and run status. With ``plan_ids`` only planned cases count as executed (generated smoke checks do not)."""
    base, cand = case_outcomes(base_records), case_outcomes(cand_records)
    for cid in plan_ids or ():  # a planned case nobody ran is not "fine": it makes the comparison INCOMPLETE
        base.setdefault(cid, {**_NO_RESULT, "messages": ["no result was recorded for this planned case"]})
        cand.setdefault(cid, {**_NO_RESULT, "messages": ["no result was recorded for this planned case"]})
    rows = []
    for cid in sorted(set(base) | set(cand)):
        rows.append({"case": cid, "baseline": (base.get(cid) or {}).get("outcome"), "candidate": (cand.get(cid) or {}).get("outcome"),
                     "delta": classify_case(base.get(cid), cand.get(cid)),
                     "baseline_evidence": (base.get(cid) or {}).get("evidence"), "candidate_evidence": (cand.get(cid) or {}).get("evidence"),
                     "messages": (cand.get(cid) or base.get(cid) or {}).get("messages", [])})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["delta"]] = counts.get(r["delta"], 0) + 1
    scoped = lambda outcomes: {k: v for k, v in outcomes.items() if plan_ids is None or k in plan_ids}
    bs, cs = summarize(scoped(base)), summarize(scoped(cand))
    new_failing = sum(1 for r in rows if r["delta"] == "NEW" and r["candidate"] == "fail")
    incomplete = counts.get("INFRA", 0) + counts.get("INCONCLUSIVE", 0) + counts.get("REMOVED", 0)
    if bs["executed"] == 0 or cs["executed"] == 0:
        status, why = "BLOCKED", "a side executed zero cases" + (" (baseline)" if bs["executed"] == 0 else "") + (" (candidate)" if cs["executed"] == 0 else "")
    elif counts.get("REGRESSION"):
        status, why = "REGRESSION", f"{counts['REGRESSION']} case(s) pass on baseline and fail on candidate"
    elif new_failing:
        status, why = "NEW_FAILURES", f"{new_failing} case(s) exist only on the candidate and fail"
    elif incomplete:
        status, why = "INCOMPLETE", f"{incomplete} case(s) could not be compared (infrastructure, blocked, inconclusive or removed)"
    elif counts.get("PRE_EXISTING"):
        status, why = "PRE_EXISTING", f"{counts['PRE_EXISTING']} case(s) fail on both sides; no regression"
    elif counts.get("FIXED"):
        status, why = "FIXED", f"{counts['FIXED']} case(s) fail on baseline and pass on candidate"
    else:
        status, why = "CLEAN", "every case passes on both sides"
    return {"status": status, "reason": why, "counts": counts, "cases": rows, "baseline_summary": bs, "candidate_summary": cs}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--baseline", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--baseline-log", default="")
    p.add_argument("--candidate-log", default="")
    p.add_argument("--perf-baseline", default="")
    p.add_argument("--perf-candidate", default="")
    p.add_argument("--noise", default="", help="file with extra regexes (one per line) for known-harmless log lines")
    p.add_argument("--plan", default="", help="plan.json: only planned cases count as executed")
    p.add_argument("--out", default="delta.json")
    a = p.parse_args(argv)
    plan_ids = {c["id"] for c in json.loads(Path(a.plan).read_text(encoding="utf-8")).get("cases", []) if c.get("id")} if a.plan else None
    result = compare(load_jsonl(a.baseline), load_jsonl(a.candidate), plan_ids)
    noise = [ln.strip() for ln in Path(a.noise).read_text().splitlines() if ln.strip()] if a.noise else None
    read = lambda f: Path(f).read_text(encoding="utf-8", errors="replace") if f and Path(f).exists() else ""
    result["new_log_errors"] = error_changes(read(a.baseline_log), read(a.candidate_log), noise)
    if a.perf_baseline and a.perf_candidate:
        result["perf_regressions"] = perf_regressions(json.loads(read(a.perf_baseline) or "{}"), json.loads(read(a.perf_candidate) or "{}"))
    Path(a.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"[qa] delta status={result['status']} ({result['reason']}) -> {a.out}", flush=True)
    print(f"::add-task-context QA_DELTA_STATUS::{result['status']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
