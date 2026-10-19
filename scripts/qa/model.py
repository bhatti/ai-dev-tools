"""Data contract and bookkeeping shared by the QA tools.

plan.json (written by the agent from the PR + acceptance criteria):
    {"criteria": [{"id": "AC1", "text": "...", "source": "issue:ABC-1|pr_description|inferred"}],
     "cases": [{"id": "C1", "ac": ["AC1"], "type": "API|UI|CLI|UNIT|MANUAL", "title": "...",
                "steps": "...", "expected": "...", "mutating": false}]}

results (JSON Lines, one record per check, appended by helpers.sh / ui_runner.py):
    {"case": "C1", "outcome": "pass|fail|blocked|not-run|inconclusive", "message": "...",
     "command": "...", "observed": "...", "screenshot": "", "infra": false}

A case's outcome is the worst of its records: fail > blocked > inconclusive > not-run > pass. A case with no
records is ``not-run``. Executed means pass or fail.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

OUTCOMES = ("pass", "fail", "blocked", "not-run", "inconclusive")
_EVIDENCE_KEYS = ("command", "observed", "screenshot", "evidence")
_SEVERITY = {"fail": 4, "blocked": 3, "inconclusive": 2, "not-run": 1, "pass": 0}
CASE_TYPES = ("API", "UI", "CLI", "UNIT", "MANUAL")
AUTOMATED_TYPES = ("API", "UI", "CLI", "UNIT")

# What kind of code a change touches, from its paths alone (a heuristic; the plan gate uses it to demand matching cases).
_UI_EXTENSIONS = {".tsx", ".jsx", ".vue", ".svelte", ".html", ".css", ".scss", ".less"}
_UI_DIRS = ("/ui/", "/frontend/", "/web/", "/client/", "/components/", "/pages/", "/views/")
_UI_DIR_LANGUAGES = {".js", ".mjs", ".cjs", ".ts"}  # in a UI directory only browser languages are UI; a Go or Python handler under web/ is backend
_SOURCE_EXTENSIONS = {".py", ".go", ".java", ".kt", ".rb", ".rs", ".cs", ".php", ".js", ".mjs", ".cjs", ".ts", ".c", ".cc", ".cpp", ".scala", ".swift"}
_TEST_PATH = re.compile(r"(^|/)(__tests__|__integration__|tests?|specs?|e2e|testdata|fixtures)/|(^|/)test_[^/]*$|[._-](test|spec)\.[^/.]+$|_test\.[^/.]+$")


def classify_changes(paths: list[str]) -> dict[str, list[str]]:
    """Split changed paths into ``ui`` and ``backend`` source files; tests, docs and configuration are neither."""
    ui, backend = [], []
    for path in paths:
        if _TEST_PATH.search(path):
            continue
        ext = Path(path).suffix.lower()
        if ext in _UI_EXTENSIONS or (ext in _UI_DIR_LANGUAGES and any(d in f"/{path}" for d in _UI_DIRS)):
            ui.append(path)
        elif ext in _SOURCE_EXTENSIONS:
            backend.append(path)
    return {"ui": ui, "backend": backend}


def change_coverage(plan: dict, changed_files: list[str]) -> tuple[list[str], list[str]]:
    """(blockers, gaps) when the plan does not exercise what the change touched: UI code needs a UI case (blocker),
    backend code needs an API, CLI or UNIT case (gap: the path heuristic is weaker there). MANUAL cases never count."""
    types = {c.get("type") for c in plan.get("cases", [])}
    changes = classify_changes(changed_files)
    blockers, gaps = [], []
    if changes["ui"] and "UI" not in types:
        blockers.append(f"the change touches UI code ({len(changes['ui'])} file(s), e.g. {changes['ui'][0]}) but the plan has no automated UI case")
    if changes["backend"] and not types & {"API", "CLI", "UNIT"}:
        gaps.append(f"the change touches backend code ({len(changes['backend'])} file(s), e.g. {changes['backend'][0]}) but the plan has no API, CLI or UNIT case")
    return blockers, gaps


def untraced_cases(plan: dict, test_sources: dict[str, str]) -> list[str]:
    """Automated case ids that appear in none of the test files (they would never produce a result). Whole tokens
    only: ``C1`` is not found in ``C10``."""
    missing = []
    for case in plan.get("cases", []):
        cid = case.get("id", "")
        if cid and case.get("type") in AUTOMATED_TYPES:
            token = re.compile(rf"(?<![A-Za-z0-9_.-]){re.escape(cid)}(?![A-Za-z0-9_.-])")
            if not any(token.search(text) for text in test_sources.values()):
                missing.append(cid)
    return missing


def load_jsonl(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    records = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a truncated line from a killed process must not hide the other results
        if isinstance(rec, dict) and rec.get("case"):
            rec["outcome"] = rec.get("outcome") if rec.get("outcome") in OUTCOMES else "inconclusive"
            records.append(rec)
    return records


def append_jsonl(path: str | Path, records: list[dict]) -> None:
    """Append records as JSON Lines (the format helpers.sh and ui_runner both write and load_jsonl reads)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def case_outcomes(records: list[dict]) -> dict[str, dict]:
    """Collapse records to one entry per case: worst outcome, joined messages, first failing evidence."""
    out: dict[str, dict] = {}
    for r in records:
        cur = out.setdefault(r["case"], {"outcome": "pass", "infra": False, "messages": [], "evidence": None, "checks": 0})
        cur["checks"] += 1
        if _SEVERITY[r["outcome"]] > _SEVERITY[cur["outcome"]]:
            cur["outcome"] = r["outcome"]
            cur["evidence"] = {k: r.get(k, "") for k in _EVIDENCE_KEYS}
        elif cur["evidence"] is None and r["outcome"] == "pass":
            cur["evidence"] = {k: r.get(k, "") for k in _EVIDENCE_KEYS}
        cur["infra"] = cur["infra"] or (bool(r.get("infra")) and r["outcome"] == "blocked")
        if r.get("message") and r["outcome"] != "pass":
            cur["messages"].append(r["message"])
    for v in out.values():
        v["infra"] = v["infra"] and v["outcome"] == "blocked"  # a case that also failed a check is a failure, not infrastructure
    return out


def validate_plan(plan: dict) -> list[str]:
    """Return human-readable problems; an empty list means the plan is usable."""
    problems: list[str] = []
    criteria = {c.get("id"): c for c in plan.get("criteria", [])}
    if not criteria:
        problems.append("plan has no criteria")
    if None in criteria or "" in criteria:
        problems.append("a criterion is missing its id")
    seen: set[str] = set()
    cases = plan.get("cases", [])
    if not cases:
        problems.append("plan has no test cases")
    for c in cases:
        cid = c.get("id", "")
        if not cid:
            problems.append("a case is missing its id")
        elif cid in seen:
            problems.append(f"duplicate case id {cid}")
        seen.add(cid)
        if c.get("type") not in CASE_TYPES:
            problems.append(f"case {cid}: type must be one of {', '.join(CASE_TYPES)}")
        for ac in c.get("ac", []):
            if ac not in criteria:
                problems.append(f"case {cid} references unknown criterion {ac}")
        if not c.get("expected"):
            problems.append(f"case {cid} has no expected result")
    return problems


def coverage(plan: dict) -> dict:
    """Map each criterion to its cases; list criteria no case covers."""
    by_ac: dict[str, list[str]] = {c["id"]: [] for c in plan.get("criteria", []) if c.get("id")}
    for case in plan.get("cases", []):
        for ac in case.get("ac", []):
            if ac in by_ac and case.get("id"):
                by_ac[ac].append(case["id"])
    return {"by_criterion": by_ac, "uncovered": [ac for ac, cs in by_ac.items() if not cs]}


def fitness(plan: dict, env: dict | None = None) -> dict:
    """Pre-run GO / CAUTION / NO-GO with the gaps that justify it (before spending an hour on a run)."""
    gaps: list[str] = []
    blockers: list[str] = []
    problems = validate_plan(plan)
    blockers += problems
    cov = coverage(plan)
    if cov["uncovered"]:
        gaps.append(f"criteria without a test case: {', '.join(cov['uncovered'])}")
    criteria = plan.get("criteria", [])
    if criteria and all(c.get("source") == "inferred" for c in criteria):
        gaps.append("all criteria are inferred (no acceptance criteria found in the PR or linked issues)")
    cases = plan.get("cases", [])
    automatable = [c for c in cases if c.get("type") in ("API", "UI", "CLI", "UNIT")]
    if cases and not automatable:
        blockers.append("no automatable case (everything is MANUAL)")
    needs_env = [c for c in automatable if c.get("type") in ("API", "UI")]
    if needs_env and not (env or {}).get("url"):
        blockers.append("API/UI cases exist but no target URL is configured")
    if sum(1 for c in cases if c.get("mutating")) and not (env or {}).get("allow_mutations"):
        gaps.append("mutating cases will be blocked without --allow-mutations")
    change_blockers, change_gaps = change_coverage(plan, (env or {}).get("changed_files") or [])
    blockers += change_blockers
    gaps += change_gaps
    if (env or {}).get("test_sources") is not None:
        blockers += [f"case {cid} appears in no test file, so it would never run" for cid in untraced_cases(plan, env["test_sources"])]
    verdict = "NO-GO" if blockers else ("CAUTION" if gaps else "GO")
    return {"verdict": verdict, "blockers": blockers, "gaps": gaps}


_NO_RESULT = {"outcome": "not-run", "infra": False, "messages": ["no result was recorded for this case"], "evidence": None, "checks": 0}


def split_by_plan(plan: dict, outcomes: dict[str, dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """(planned, extra): one entry per planned case (``not-run`` when nothing was recorded) and the results for ids
    the plan does not contain (e.g. generated smoke checks). Only planned cases count toward verification."""
    planned = {c["id"]: outcomes.get(c["id"], dict(_NO_RESULT)) for c in plan.get("cases", []) if c.get("id")}
    return planned, {k: v for k, v in outcomes.items() if k not in planned}


def ac_status(criterion_id: str, plan: dict, outcomes: dict[str, dict]) -> str:
    """met | partial | not met | unverifiable for one criterion, from its cases' outcomes."""
    case_ids = [c["id"] for c in plan.get("cases", []) if criterion_id in c.get("ac", [])]
    results = [outcomes[c]["outcome"] for c in case_ids if c in outcomes]
    executed = [r for r in results if r in ("pass", "fail")]
    if not executed:
        return "unverifiable"
    if all(r == "pass" for r in executed) and len(executed) == len(case_ids):
        return "met"
    if all(r == "fail" for r in executed):
        return "not met"
    return "partial"


def summarize(outcomes: dict[str, dict]) -> dict:
    counts = {o: 0 for o in OUTCOMES}
    for v in outcomes.values():
        counts[v["outcome"]] += 1
    executed = counts["pass"] + counts["fail"]
    return {"counts": counts, "executed": executed, "total": len(outcomes),
            "pass_rate": round(counts["pass"] / executed, 4) if executed else None}


def main(argv: list[str] | None = None) -> int:
    """Validate a plan before spending time on a run: ``python3 -m scripts.qa.model plan.json [--url U] [--allow-mutations]``."""
    import argparse
    import sys

    p = argparse.ArgumentParser(description=main.__doc__.splitlines()[0])
    p.add_argument("plan")
    p.add_argument("--url", default="")
    p.add_argument("--allow-mutations", action="store_true")
    p.add_argument("--pr-context", default="", help="pr-context.json: its changed_files must be exercised by the plan")
    p.add_argument("--tests", nargs="*", default=[], help="test files (api-tests.sh, ui-spec.json): every automated case id must appear in one; .sh files are syntax-checked")
    a = p.parse_args(argv)
    try:
        plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"cannot read plan: {e}"}))
        return 2
    env: dict = {"url": a.url, "allow_mutations": a.allow_mutations}
    try:
        if a.pr_context:
            env["changed_files"] = json.loads(Path(a.pr_context).read_text(encoding="utf-8")).get("changed_files") or []
        if a.tests:
            env["test_sources"] = {t: Path(t).read_text(encoding="utf-8") for t in a.tests}
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"cannot read input: {e}"}))
        return 2
    result = fitness(plan, env)
    for t in a.tests:
        if t.endswith(".sh"):
            check = subprocess.run(["bash", "-n", t], capture_output=True, text=True)
            if check.returncode != 0:
                result["blockers"].append(f"{t} has a shell syntax error: {check.stderr.strip()[:200]}")
                result["verdict"] = "NO-GO"
    result["coverage"] = coverage(plan)
    print(json.dumps(result, indent=2))
    print(f"::add-task-context QA_FITNESS::{result['verdict']}", flush=True)
    return 2 if result["verdict"] == "NO-GO" else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
