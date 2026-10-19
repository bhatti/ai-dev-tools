"""Render the QA report (md, html, json) and the final status line from plan, results and delta.

Rules enforced here rather than left to the model:
  * only cases in the plan count; a planned case with no result is ``not-run``, and zero executed planned cases
    means BLOCKED/ERROR, never a clean report (generated smoke checks are listed separately)
  * regression mode without a delta is BLOCKED, as is an invalid plan or an environment that failed to start
  * each acceptance criterion is shown verbatim with the cases that cover it and a status:
    met / partial / not met / unverifiable
  * spec compliance, scope creep and quality are separate axes (one never masks another)
  * blocked and not-run cases are listed, never counted as passed; all untrusted text is escaped and redacted

Usage:
    python3 -m scripts.qa.report --plan plan.json --candidate qa-out/candidate/results.jsonl \
        [--mode regression --delta delta.json] [--pr pr-context.json] [--findings findings.json] \
        [--env-failure qa-out/baseline/env-failure.json]... --target-url URL --out-dir reports
Evidence files referenced by the results (screenshots, response bodies) are copied, redacted, into
<out-dir>/evidence/. Only files under --evidence-root (default: the directory holding the side folders) are copied.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.common.report_bundle import collect_evidence, md_cell as _cell, write_bundle
from scripts.qa import model

_READINESS_SIGNAL = {"Ready": "DONE", "Ready with concerns": "DONE_WITH_CONCERNS", "Not ready": "DONE_WITH_CONCERNS", "Blocked": "BLOCKED"}


def assess(plan: dict, cand_records: list[dict], delta: dict | None, env_failures: list[dict], findings: dict, mode: str = "verify",
           changed_files: list[str] | None = None) -> dict:
    """Compute readiness and the reasons behind it."""
    all_outcomes = model.case_outcomes(cand_records)
    outcomes, extra = model.split_by_plan(plan, all_outcomes)
    summ = model.summarize(outcomes)
    statuses = {c["id"]: model.ac_status(c["id"], plan, outcomes) for c in plan.get("criteria", []) if c.get("id")}
    cov = model.coverage(plan)
    concerns: list[str] = []
    problems: list[str] = []
    blockers = [f"plan is invalid: {p}" for p in model.validate_plan(plan)]
    blockers += [f"{e.get('side', '?')} environment failed to start: {e.get('error', '')}" for e in env_failures]
    change_blockers, change_gaps = model.change_coverage(plan, changed_files or [])
    blockers += change_blockers
    if mode == "regression" and delta is None:
        blockers.append("regression mode needs a delta (baseline vs candidate comparison) but none was provided")
    if summ["executed"] == 0:
        blockers.append("no planned case executed on the candidate")
    if delta and delta["status"] == "BLOCKED":
        blockers.append(f"regression comparison blocked: {delta['reason']}")
    automated = {c["id"]: c for c in plan.get("cases", []) if c.get("id") and c.get("type") in model.AUTOMATED_TYPES}
    silent = [cid for cid in automated if cid not in all_outcomes]
    if silent:
        problems.append(f"no result was recorded for case(s) {', '.join(silent)}: their test never ran or never reported")
    if summ["counts"]["fail"]:
        problems.append(f"{summ['counts']['fail']} case(s) failed")
    not_met = [a for a, s in statuses.items() if s == "not met"]
    if not_met:
        problems.append(f"criteria not met: {', '.join(not_met)}")
    if delta and delta["status"] in ("REGRESSION", "NEW_FAILURES"):
        problems.append(delta["reason"])
    if delta and delta.get("perf_regressions"):
        problems.append(f"{len(delta['perf_regressions'])} endpoint(s) slower by more than 20%")
    if delta and delta.get("new_log_errors"):
        concerns.append(f"{len(delta['new_log_errors'])} new or increased error pattern(s) in candidate logs")
    if delta and delta["status"] in ("INCOMPLETE", "PRE_EXISTING"):
        concerns.append(delta["reason"])
    partial = [a for a, s in statuses.items() if s in ("partial", "unverifiable")]
    if partial:
        concerns.append(f"criteria partially or not verifiable: {', '.join(partial)}")
    concerns += change_gaps
    if cov["uncovered"]:
        concerns.append(f"criteria without a test case: {', '.join(cov['uncovered'])}")
    c = summ["counts"]
    if c["blocked"] or c["not-run"] or c["inconclusive"]:
        concerns.append(f"{c['blocked']} blocked, {c['not-run']} not run, {c['inconclusive']} inconclusive case(s)")
    if plan.get("criteria") and all(cr.get("source") == "inferred" for cr in plan["criteria"]):
        concerns.append("all criteria are inferred (the PR and linked issues had none)")
    if findings.get("scope_creep"):
        concerns.append(f"{len(findings['scope_creep'])} behavior(s) in the diff not covered by any criterion")
    readiness = "Blocked" if blockers else ("Not ready" if problems else ("Ready with concerns" if concerns else "Ready"))
    return {"readiness": readiness, "blockers": blockers, "problems": problems, "concerns": concerns, "ac_status": statuses,
            "coverage": cov, "summary": summ, "outcomes": outcomes, "extra": extra}


def _evidence_links(evidence: dict | None, links: dict[str, str]) -> str:
    """Markdown links for the screenshot / evidence file of one case (empty when none were collected)."""
    parts = []
    if evidence and links.get(evidence.get("screenshot", "")):
        parts.append(f"[screenshot]({links[evidence['screenshot']]})")
    if evidence and links.get(evidence.get("evidence", "")):
        parts.append(f"[evidence]({links[evidence['evidence']]})")
    return " ".join(parts)


def render_markdown(plan: dict, assessment: dict, meta: dict, delta: dict | None, findings: dict,
                    links: dict[str, str] | None = None) -> str:
    links = links or {}
    a = assessment
    reasons = a["blockers"] + a["problems"] + a["concerns"]
    lines = [f"# QA report: {_cell(meta.get('title') or meta.get('ref') or 'local changes')}", "",
             f"> **{a['readiness'].upper()}**" + (f": {_cell('; '.join(reasons))}" if reasons else ": every criterion verified, no failures"), ""]
    s = a["summary"]
    rows = [("Mode", meta.get("mode", "verify")), ("PR / ref", meta.get("url") or meta.get("ref") or "n/a"),
            ("Criteria source", meta.get("ac_source") or "n/a"), ("Target", meta.get("target_url") or "n/a")]
    if meta.get("baseline_url"):
        rows.append(("Baseline", meta["baseline_url"]))
    rows += [("Executed", f"{s['executed']} of {s['total']} planned case(s): {s['counts']['pass']} pass, {s['counts']['fail']} fail, "
                          f"{s['counts']['blocked']} blocked, {s['counts']['not-run']} not run, {s['counts']['inconclusive']} inconclusive"),
             ("Pass rate (executed only)", "n/a" if s["pass_rate"] is None else f"{s['pass_rate']:.0%}")]
    lines += ["| Field | Value |", "| --- | --- |"] + [f"| {_cell(k)} | {_cell(v)} |" for k, v in rows]

    lines += ["", "## Acceptance criteria traceability", "", "| AC | Criterion (verbatim) | Source | Cases | Status |", "| --- | --- | --- | --- | --- |"]
    for c in plan.get("criteria", []):
        cases = ", ".join(a["coverage"]["by_criterion"].get(c.get("id"), [])) or "none"
        lines.append(f"| {_cell(c.get('id', ''))} | {_cell(c.get('text', ''))} | {_cell(c.get('source', ''))} | {_cell(cases)} | "
                     f"{_cell(a['ac_status'].get(c.get('id'), 'unverifiable'))} |")

    lines += ["", "## Test cases", "", "| Case | Type | AC | Outcome | Evidence |", "| --- | --- | --- | --- | --- |"]
    for case in plan.get("cases", []):
        o = a["outcomes"].get(case.get("id"))
        outcome = o["outcome"] if o else "not-run"
        ev = ""
        if o and o.get("evidence"):
            e = o["evidence"]
            ev = e.get("observed") or e.get("command") or ""
        if o and o["messages"]:
            ev = f"{'; '.join(o['messages'][:2])} ({ev})"
        ev_links = _evidence_links(o.get("evidence") if o else None, links)
        lines.append(f"| {_cell(str(case.get('id', '')) + ' ' + str(case.get('title', '')))} | {_cell(case.get('type', ''))} | "
                     f"{_cell(', '.join(case.get('ac', [])))} | {outcome}{' (infra)' if o and o['infra'] else ''} | {_cell(ev)[:300]} {ev_links} |")

    if a["extra"]:
        lines += ["", "## Additional checks (not part of the plan; they do not count toward verification)", "",
                  "| Check | Outcome | Detail |", "| --- | --- | --- |"]
        lines += [f"| {_cell(cid)} | {v['outcome']} | {_cell('; '.join(v['messages'][:2]))} |" for cid, v in sorted(a["extra"].items())]

    if delta:
        lines += ["", "## Regression delta (baseline vs candidate)", "", f"Status: **{_cell(delta['status'])}**: {_cell(delta['reason'])}", "",
                  "| Case | Baseline | Candidate | Delta | Evidence (baseline / candidate) |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {_cell(r['case'])} | {_cell(r['baseline'] or 'n/a')} | {_cell(r['candidate'] or 'n/a')} | {_cell(r['delta'])} | "
                  f"{_evidence_links(r.get('baseline_evidence'), links) or 'n/a'} / {_evidence_links(r.get('candidate_evidence'), links) or 'n/a'} |"
                  for r in delta["cases"]]
        if delta.get("new_log_errors"):
            lines += ["", "### New or increased error patterns in candidate logs", "", "| Kind | Count (baseline) | Sample |", "| --- | --- | --- |"]
            lines += [f"| {_cell(e.get('kind', 'new'))} | {e['count']} ({e.get('baseline_count', 0)}) | {_cell(e['sample'])} |" for e in delta["new_log_errors"][:20]]
        if delta.get("perf_regressions"):
            lines += ["", "### Latency regressions (p50)", "", "| Endpoint | Baseline ms | Candidate ms | Ratio |", "| --- | --- | --- | --- |"]
            lines += [f"| {_cell(p['endpoint'])} | {p['baseline_p50_ms']} | {p['candidate_p50_ms']} | {p['ratio']}x |" for p in delta["perf_regressions"]]

    counts = {k: sum(1 for v in a["ac_status"].values() if v == k) for k in ("met", "partial", "not met", "unverifiable")}
    lines += ["", "## Review axes (reported separately)", "", f"- **Spec compliance:** {counts['met']} met, {counts['partial']} partial, {counts['not met']} not met, "
              f"{counts['unverifiable']} unverifiable of {len(a['ac_status'])} criteria"]
    creep = findings.get("scope_creep") or []
    lines.append(f"- **Scope creep:** {len(creep)} behavior(s) in the diff that no criterion asks for" if creep else "- **Scope creep:** none found")
    lines += [f"  - {_cell(c.get('behavior', ''))} ({_cell(str(c.get('file', '')) + ':' + str(c.get('line', '?')))}) {_cell(c.get('note', ''))}" for c in creep]
    quality = findings.get("quality") or []
    lines.append(f"- **Quality notes:** {len(quality)}" if quality else "- **Quality notes:** none recorded")
    lines += [f"  - [{_cell(q.get('severity', 'info'))}] {_cell(q.get('title', ''))} ({_cell(str(q.get('file', '')) + ':' + str(q.get('line', '?')))}) {_cell(q.get('detail', ''))}" for q in quality]

    unrun = [(cid, v) for cid, v in a["outcomes"].items() if v["outcome"] in ("blocked", "not-run", "inconclusive")]
    if unrun:
        lines += ["", "## Blocked / not run / inconclusive (not counted as passed)", ""]
        lines += [f"- {_cell(cid)}: {v['outcome']}{' (infrastructure)' if v['infra'] else ''}: {_cell('; '.join(v['messages'][:2]) or 'no detail')}" for cid, v in unrun]

    not_judged = findings.get("not_judged") or meta.get("not_judged") or ["Platforms, browsers and data volumes other than those tested", "Non-functional properties not covered by a criterion"]
    lines += ["", "## Not judged", ""] + [f"- {_cell(n)}" for n in not_judged]
    return "\n".join(lines) + "\n"


def status_line(assessment: dict, delta: dict | None) -> dict:
    a = assessment
    reasons = a["blockers"] + a["problems"] + a["concerns"]
    return {"status": "ERROR" if a["readiness"] == "Blocked" else "DONE", "signal": _READINESS_SIGNAL[a["readiness"]],
            "readiness": a["readiness"], "executed": a["summary"]["executed"], "failures": a["summary"]["counts"]["fail"],
            "delta": delta["status"] if delta else None, "summary": "; ".join(reasons) or "all criteria verified"}


def _evidence_paths(outcomes: dict[str, dict], delta: dict | None) -> list[str]:
    paths = [e.get(k, "") for v in outcomes.values() for e in [v.get("evidence") or {}] for k in ("screenshot", "evidence")]
    for row in (delta or {}).get("cases", []):
        paths += [e.get(k, "") for e in (row.get("baseline_evidence") or {}, row.get("candidate_evidence") or {}) for k in ("screenshot", "evidence")]
    return paths


def write_outputs(plan: dict, cand_records: list[dict], delta: dict | None, env_failures: list[dict], findings: dict, meta: dict,
                  out_dir: Path, evidence_roots: list[Path] | None = None) -> dict:
    a = assess(plan, cand_records, delta, env_failures, findings, meta.get("mode", "verify"), meta.get("changed_files"))
    links = collect_evidence(out_dir, _evidence_paths({**a["outcomes"], **a["extra"]}, delta), evidence_roots or [])
    line = status_line(a, delta)
    result = {**line, "ac_status": a["ac_status"], "summary_stats": a["summary"], "delta": delta, "meta": meta, "evidence_files": sorted(links.values())}
    write_bundle(out_dir, "QA report", render_markdown(plan, a, meta, delta, findings, links), result)
    return line


def _load_json(path: str, what: str) -> dict:
    """Read a JSON file the caller named explicitly; a missing or malformed file is an error, never an empty default."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"cannot read {what} {path!r}: {e}") from e


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--plan", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--delta", default="")
    p.add_argument("--pr", default="", help="pr-context.json from scripts.common.pr_context")
    p.add_argument("--findings", default="")
    p.add_argument("--env-failure", action="append", default=[], help="env-failure.json files; a file that does not exist means the environment started")
    p.add_argument("--mode", default="verify", choices=["verify", "regression"])
    p.add_argument("--target-url", default="")
    p.add_argument("--baseline-url", default="")
    p.add_argument("--evidence-root", action="append", default=[])
    p.add_argument("--out-dir", default="reports")
    a = p.parse_args(argv)
    try:
        plan = _load_json(a.plan, "plan")
        delta = _load_json(a.delta, "delta") if a.delta else None
        pr = _load_json(a.pr, "pr context") if a.pr else {}
        findings = _load_json(a.findings, "findings") if a.findings else {}
        env_failures = [_load_json(f, "env-failure") for f in a.env_failure if Path(f).exists()]
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    cand = Path(a.candidate)
    roots = [Path(r) for r in a.evidence_root] or [cand.resolve().parent.parent]
    meta = {"mode": a.mode, "title": (pr.get("pr") or {}).get("title", ""), "url": (pr.get("pr") or {}).get("url", ""),
            "ref": a.pr, "ac_source": pr.get("ac_source", ""), "changed_files": pr.get("changed_files") or [], "target_url": a.target_url, "baseline_url": a.baseline_url}
    line = write_outputs(plan, model.load_jsonl(cand), delta, env_failures, findings, meta, Path(a.out_dir), roots)
    print(f"[qa] wrote {a.out_dir}/report.md, report.html, result.json", flush=True)
    print(json.dumps(line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
