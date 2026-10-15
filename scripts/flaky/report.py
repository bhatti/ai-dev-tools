"""Render the flaky-test report (md, html, json) and the final status line from rerun/classify output.

Wording rules enforced here, not left to the model:
  * CANNOT_REPRODUCE is stated as ``0/N failures (95% upper bound X%)`` and never as "not flaky".
  * ENV_BLOCKED means nothing was verified, so the status is ERROR, never a pass.
  * Output excerpts are redacted before they enter the report.

Usage:
    python3 -m scripts.flaky.report --runs runs.json [--findings findings.json] --test "tests/a.test.js::adds item" \
        [--runner jest] [--rev SHA] [--ticket URL] [--out-dir reports] [--not-judged "Windows"]...
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
from pathlib import Path

from scripts.common.redact import redact
from scripts.common.report_bundle import md_cell as _cell, write_bundle

_SIGNAL = {
    "FLAKY": ("DONE", "DONE"), "DETERMINISTIC_FAIL": ("DONE", "DONE"),
    "CANNOT_REPRODUCE": ("DONE", "DONE_WITH_CONCERNS"), "INCONCLUSIVE": ("DONE", "DONE_WITH_CONCERNS"), "ENV_BLOCKED": ("ERROR", "BLOCKED"),
}
_HEADLINE = {
    "FLAKY": "FLAKY: intermittent failures reproduced",
    "DETERMINISTIC_FAIL": "DETERMINISTIC FAILURE: fails every run, this is a bug not a flake",
    "CANNOT_REPRODUCE": "CANNOT REPRODUCE: no failure observed (not proof the test is stable)",
    "INCONCLUSIVE": "INCONCLUSIVE: failed in too few runs to tell a flake from a bug",
    "ENV_BLOCKED": "ENVIRONMENT BLOCKED: no test was executed, nothing was verified",
}


def _rate(s: dict) -> str:
    if s["failure_rate"] is not None:
        return f"{s['failure_rate']:.1%} (95% CI {s['wilson95'][0]:.1%} to {s['wilson95'][1]:.1%})"
    return "n/a" if s["verdict"] == "ENV_BLOCKED" else "not estimated (the loop stopped as soon as pass and fail were both seen)"


def _fence(text: str) -> str:
    """A code fence longer than any backtick run inside ``text`` (failing output must not be able to close it)."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render_markdown(runs: dict, findings: list[dict], meta: dict) -> str:
    s = runs["summary"]
    lines = [f"# Flaky test report: {_cell(meta['test'])}", "", f"> **{_HEADLINE[s['verdict']]}**", "",
             f"> {s['statement']}", "", "| Field | Value |", "| --- | --- |"]
    rows = [("Test", meta["test"]), ("Runner", meta.get("runner") or "n/a"),
            ("Command", " ".join(runs.get("argv_full", []))), ("Isolated command", " ".join(runs.get("argv_isolated") or []) or "n/a"),
            ("Revision", meta.get("rev") or "unknown"), ("Platform", meta.get("platform") or platform.platform()),
            ("Runs", f"{s['executed']} executed / {s['requested_runs']} requested ({s['stop_reason']})"),
            ("Passes / failures / env", f"{s['passes']} / {s['failures']} / {s['env_failures']}"),
            ("Failure rate", _rate(s)),
            ("Duration", f"{s['total_duration_s']}s")]
    if meta.get("ticket"):
        rows.append(("Ticket", meta["ticket"]))
    lines += [f"| {k} | {_cell(v)} |" for k, v in rows]

    lines += ["", "## Runs by phase", "", "| Phase | Runs | Pass | Fail | Env |", "| --- | --- | --- | --- | --- |"]
    for phase in sorted({i["phase"] for i in runs["iterations"]}):
        its = [i for i in runs["iterations"] if i["phase"] == phase]
        lines.append(f"| {phase} | {len(its)} | {sum(1 for i in its if i['failure_class'] is None)} | "
                     f"{sum(1 for i in its if i['failure_class'] in ('test', 'timeout', 'build'))} | "
                     f"{sum(1 for i in its if i['failure_class'] == 'env')} |")

    if s["failure_signatures"]:
        lines += ["", "## Failure signatures", "", "| Count | Normalized first failure line |", "| --- | --- |"]
        lines += [f"| {f['count']} | `{_cell(f['fingerprint'])}` |" for f in s["failure_signatures"]]

    lines += ["", "## Diagnosis (hypotheses, not proven causes)", ""]
    if findings:
        lines += ["| Severity | Signature | Where | Evidence | Recommendation |", "| --- | --- | --- | --- | --- |"]
        for f in findings:
            where = f"{f['file']}:{f['line']}" if f.get("file") else "runtime output"
            ev = f.get("snippet") or f["evidence"]
            lines.append(f"| {f['severity']} | {f['id']} | {_cell(where)} | `{_cell(ev)}` | {_cell(f['recommendation'])} |")
    else:
        lines.append("No known signature matched. Documented as unexplained; see attempts below.")
    if meta.get("attempts"):
        lines += ["", "### Hypotheses tested", ""] + [f"{i}. {_cell(a)}" for i, a in enumerate(meta["attempts"], 1)]

    fail = next((i for i in runs["iterations"] if i["failure_class"] in ("test", "timeout", "build")), None)
    if fail:
        body = redact(fail["output_tail"]).strip()[:3000]
        fence = _fence(body)
        lines += ["", f"## First failing run (#{fail['index']}, {fail['phase']}, exit {fail['exit_code']})", "", fence, body, fence]

    not_judged = meta.get("not_judged") or ["Other operating systems/architectures", "CI-only environment differences",
                                            "Failure rates under production load"]
    lines += ["", "## Not judged", ""] + [f"- {_cell(n)}" for n in not_judged]
    return "\n".join(lines) + "\n"


def status_line(runs: dict) -> dict:
    s = runs["summary"]
    status, signal = _SIGNAL[s["verdict"]]
    return {"status": status, "signal": signal, "verdict": s["verdict"], "runs": s["completed"], "failures": s["failures"],
            "summary": f"{s['verdict']}: {s['statement']}"}


def write_outputs(runs: dict, findings: list[dict], meta: dict, out_dir: Path) -> dict:
    result = {**status_line(runs), "findings": findings, "meta": meta, "summary_stats": runs["summary"]}
    write_bundle(out_dir, "Flaky test report", render_markdown(runs, findings, meta), result)
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs", required=True)
    p.add_argument("--findings", default="")
    p.add_argument("--test", required=True)
    p.add_argument("--runner", default="")
    p.add_argument("--rev", default="")
    p.add_argument("--ticket", default="")
    p.add_argument("--out-dir", default="reports")
    p.add_argument("--not-judged", action="append", default=[])
    p.add_argument("--attempt", action="append", default=[], help="a hypothesis that was tested (repeatable)")
    args = p.parse_args(argv)
    runs = json.loads(Path(args.runs).read_text(encoding="utf-8"))
    findings = json.loads(Path(args.findings).read_text(encoding="utf-8"))["findings"] if args.findings else []
    meta = {"test": args.test, "runner": args.runner, "rev": args.rev, "ticket": args.ticket,
            "not_judged": args.not_judged, "attempts": args.attempt}
    result = write_outputs(runs, findings, meta, Path(args.out_dir))
    print(f"[flaky] wrote {args.out_dir}/report.md, report.html, result.json", flush=True)
    print(json.dumps({k: result[k] for k in ("status", "signal", "verdict", "runs", "failures", "summary")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
