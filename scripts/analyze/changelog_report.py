# SPDX-License-Identifier: LGPL-2.1-or-later
"""The audit's "Changelog & Blast Radius": merge the model's changelog with deterministic facts, validate, render.

The model (specialist ``changelog-blast-radius``) writes ``reports/changelog_model.json``: feature titles and summaries,
which PRs belong together, semantic breaking-change judgements and test ideas. Owners, blast radius and signals come from
``change_impact`` and always win: the model cannot lower a blast radius or remove a signal. Without a usable model file
the changelog is built from the facts alone, so the section always renders.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.common.report_bundle import md_cell
from scripts.common.report_renderer import write_markdown_report

OTHER = "Other notable changes"
_CONF = ("high", "medium", "low")
_BLAST = ("low", "medium", "high")
_TEST_HINTS = {
    "field_removed": "Consumer contract tests against the new schema (buf breaking / protobuf compatibility check); confirm no client still reads the field",
    "field_renamed": "Consumer contract tests; keep the old name as a deprecated alias until clients migrate",
    "field_renumbered": "Wire-compatibility test: decode payloads produced by the previous release",
    "field_type_changed": "Wire-compatibility test with payloads from the previous release; check every consumer parses the new type",
    "enum_value_removed": "Exhaustive-switch tests in every consumer and a test that stored/persisted values of the removed member still load",
    "enum_value_changed": "Persisted-value and wire-format tests: stored data and clients must still map the old value",
    "rpc_removed": "Contract tests for every caller of the removed rpc; confirm callers were migrated first",
    "type_removed": "Build all consumers against the new contract; contract tests for each",
    "endpoint_removed": "Contract tests (oasdiff / OpenAPI diff) and traffic check that the endpoint has no remaining callers",
    "route_removed": "Contract tests and access-log check that the route has no remaining callers; keep a redirect or deprecation period",
    "route_method_changed": "Contract tests for every caller; verify clients using the old method get a clear error",
    "required_added": "Consumer contract tests: requests without the new required property must still be accepted or fail clearly",
    "schema_field_removed": "Consumer contract tests (oasdiff / JSON schema compatibility) for readers of the removed property",
    "type_changed": "Consumer contract tests with payloads using the old type",
    "status_code_changed": "Client tests for error handling: callers that branch on the old status code",
    "required_param_added": "Compile and run every caller of the changed function; add a default or a compatibility overload",
    "param_removed": "Compile and run every caller of the changed function",
    "public_symbol_removed": "Build all dependents; search for remaining usages before removing",
    "db_drop": "Expand/contract migration rehearsal on a copy of production data; confirm nothing reads the dropped object and a rollback exists",
    "db_rename": "Expand/contract rehearsal: old and new application versions must both run against the schema during rollout",
    "db_type_changed": "Migration rehearsal on production-sized data (locks, rewrite time) and a compatibility test for old readers",
    "db_not_null_no_default": "Rehearse on production-sized data; old application versions that omit the column will fail inserts",
    "db_not_null_added": "Check existing rows and old writers before enforcing NOT NULL; rehearse on production-sized data",
    "db_constraint_added": "Validate existing data against the new constraint and test writers that may violate it",
    "default_changed": "Canary or staged rollout with metrics: behaviour changes for everyone relying on the old default, limit or timeout",
    "declared_breaking": "Read the migration notes; run consumer regression suites against this change before release",
    "contract_changed": "Contract diff (oasdiff / buf breaking) between the previous and this release",
    "migration_changed": "Migration rehearsal on a production-sized copy; confirm rollback",
    "config_changed": "Review changed defaults/keys; canary the rollout",
    "api_changed": "Contract tests for the changed endpoints' consumers",
    "enum_or_model_changed": "Serialization round-trip and exhaustive-switch tests in consumers",
    "deprecation": "Track callers of the deprecated API and set a removal date",
}


def _ints(v) -> list[int]:
    out = []
    for x in v if isinstance(v, list) else [v]:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def _strs(v) -> list[str]:
    return [str(x) for x in (v if isinstance(v, list) else [v] if v else []) if isinstance(x, (str, int)) and str(x).strip()]


def _coerce(data) -> dict | None:
    """The model's changelog coerced to the expected shape; None when it is not usable."""
    if not isinstance(data, dict) or not isinstance(data.get("features"), list):
        return None
    features = []
    for f in data["features"]:
        if not isinstance(f, dict) or not f.get("title"):
            continue
        breaking = [{"kind": str(b.get("kind") or "semantic"), "confidence": b["confidence"] if b.get("confidence") in _CONF else "low",
                     "evidence": str(b.get("evidence") or ""), "consumers": _strs(b.get("consumers")), "migration": str(b.get("migration") or ""),
                     "pr": (_ints(b.get("pr")) or [0])[0]} for b in f.get("breaking") or [] if isinstance(b, dict)]
        tests = [{"team": str(t["team"]), "what": str(t["what"])} for t in f.get("tests") or [] if isinstance(t, dict) and t.get("team") and t.get("what")]
        features.append({"title": str(f["title"]), "summary": str(f.get("summary") or ""), "category": str(f.get("category") or ""),
                         "prs": _ints(f.get("prs")), "breaking": breaking, "tests": tests})
    return {"features": features}


def load_model(reports_dir: Path) -> dict | None:
    """``reports/changelog_model.json`` coerced; None when absent, malformed or empty of features."""
    try:
        return _coerce(json.loads((reports_dir / "changelog_model.json").read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def _level(prs: list[dict]) -> str:
    return max((p["blast"]["level"] for p in prs), key=_BLAST.index, default="low")


def _significant(r: dict) -> bool:
    return r["blast"]["level"] != "low" or any(s["confidence"] != "low" for s in r["signals"])


def _feature(title: str, summary: str, category: str, prs: list[dict], model_breaking: list[dict], model_tests: list[dict]) -> dict:
    teams = list(dict.fromkeys(t for p in prs for t in p["teams"]))
    internal = sorted({t for p in prs for t in p["blast"]["internal"]})
    breaking = []
    seen = set()
    for p in prs:
        for s in p["signals"]:
            if s["confidence"] == "low" and s["kind"] != "declared_breaking":
                continue
            seen.add((p["number"], s["kind"], s["evidence"][:60]))
            breaking.append({"pr": p["number"], "kind": s["kind"], "confidence": s["confidence"], "evidence": s["evidence"], "file": s["file"],
                             "consumers": p["blast"]["internal"], "migration": "", "source": "signal"})
    for b in model_breaking:
        key = (b["pr"], b["kind"], b["evidence"][:60])
        if key not in seen and (b["pr"] == 0 or any(b["pr"] == p["number"] for p in prs)):
            seen.add(key)
            breaking.append({**b, "file": "", "source": "model"})
    breaking.sort(key=lambda b: (_CONF.index(b["confidence"]), b["pr"]))
    tests = list(model_tests)
    for b in breaking:
        hint = _TEST_HINTS.get(b["kind"])
        for team in (b["consumers"] or teams):
            if hint and {"team": team, "what": hint} not in tests:
                tests.append({"team": team, "what": hint})
    return {"title": title, "summary": summary, "category": category, "prs": [p["number"] for p in prs], "teams": teams,
            "owner_source": prs[0]["owner_source"] if prs else "area",
            "blast": {"level": _level(prs), "external": any(p["blast"]["external"] for p in prs), "internal": internal,
                      "reasons": list(dict.fromkeys(r for p in prs for r in p["blast"]["reasons"]))[:5]},
            "breaking": breaking[:12], "tests": tests[:12]}


def merge(facts: dict, model: dict | None) -> dict:
    model = _coerce(model) if model is not None else None
    by_num = {r["number"]: r for r in facts["prs"]}
    features, used = [], set()
    for f in (model or {}).get("features", []):
        prs = [by_num[n] for n in f["prs"] if n in by_num]
        if prs:
            used |= {p["number"] for p in prs}
            features.append(_feature(f["title"], f["summary"], f["category"], prs, f["breaking"], f["tests"]))
    leftovers = [r for r in facts["prs"] if r["number"] not in used and _significant(r)]
    if model is None:
        features += [_feature(r["title"], "", "", [r], [], []) for r in leftovers]
    elif leftovers:
        features.append(_feature(OTHER, "", "", leftovers, [], []))
    rollup = sorted(({**b, "feature": f["title"], "teams": f["teams"]} for f in features for b in f["breaking"]), key=lambda b: (_CONF.index(b["confidence"]), b["pr"]))
    low = sum(1 for r in facts["prs"] for s in r["signals"] if s["confidence"] == "low")
    teams = {t: {**s, "features": [f["title"] for f in features if t in f["teams"]]} for t, s in facts["teams"].items()}
    return {"mode": "model" if model is not None else "facts", "features": features, "breaking_changes": rollup, "teams": teams, "meta": facts["meta"],
            "summary": {"features": len(features), "teams": len(teams), "breaking_high": sum(b["confidence"] == "high" for b in rollup),
                        "breaking_medium": sum(b["confidence"] == "medium" for b in rollup), "candidates_low": low}}


def render_markdown(out: dict, heading: str = "## Changelog & Blast Radius") -> str:
    s, m = out["summary"], out["meta"]
    if not out["features"]:
        return f"{heading}\n\nNo notable changes: no PR in this audit had a medium or high blast radius or a breaking-change signal.\n"
    lines = [heading, "",
             f"*{s['features']} change(s) across {s['teams']} team(s) · {s['breaking_high']} high and {s['breaking_medium']} medium confidence breaking-change "
             f"candidate(s) · diffs read for {m['diff_prs']} of {m['prs']} PRs. Candidates are a review aid, not a gate: they come from heuristics and a model's judgement.*", ""]
    if out["breaking_changes"]:
        lines += ["### Breaking-change candidates", "", "| Confidence | Change | PR | Teams | Used by | Evidence | What to do |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for b in out["breaking_changes"][:40]:
            lines.append(f"| {b['confidence']} | {md_cell(b['kind'])} ({md_cell(b['feature'])}) | #{b['pr']} | {md_cell(', '.join(b['teams']))} | "
                         f"{md_cell(', '.join(b['consumers']) or 'unknown')} | {md_cell(b['evidence'])[:160]} | {md_cell(b.get('migration') or _TEST_HINTS.get(b['kind'], ''))[:200]} |")
        lines.append("")
    lines += ["### Blast radius by team", "", "| Team | PRs | High blast | Breaking candidates | Changes |", "| --- | --- | --- | --- | --- |"]
    for t, st in sorted(out["teams"].items(), key=lambda kv: (-kv[1]["high_blast"], -kv[1]["prs"], kv[0])):
        lines.append(f"| {md_cell(t)} | {st['prs']} | {st['high_blast']} | {st['breaking_candidates']} | {md_cell('; '.join(st['features'][:4]))} |")
    lines += ["", "### Features by team", ""]
    by_team: dict[str, list[dict]] = {}
    for f in out["features"]:
        for t in f["teams"][:1] or ["unowned"]:
            by_team.setdefault(t, []).append(f)
    for t, feats in sorted(by_team.items()):
        lines += [f"#### {md_cell(t)}", ""]
        for f in feats:
            b = f["blast"]
            reach = ("external" if b["external"] else "internal") + (f"; used by {md_cell(', '.join(b['internal']))}" if b["internal"] else "")
            lines.append(f"- **{md_cell(f['title'])}** (PRs {', '.join('#' + str(n) for n in f['prs'][:8])}) · blast **{b['level']}** ({reach})"
                         + (f": {md_cell(f['summary'])}" if f["summary"] else ""))
            lines += [f"  - Why: {md_cell('; '.join(b['reasons']))}"] if b["reasons"] else []
            lines += [f"  - Breaking ({x['confidence']}): {md_cell(x['kind'])}: {md_cell(x['evidence'])[:140]}" for x in f["breaking"][:4]]
        lines.append("")
    tests = {(t["team"], t["what"]) for f in out["features"] for t in f["tests"]}
    if tests:
        lines += ["### Suggested tests", "", "| Team | Test | Why |", "| --- | --- | --- |"]
        by = {(t["team"], t["what"]): f["title"] for f in out["features"] for t in f["tests"]}
        lines += [f"| {md_cell(team)} | {md_cell(what)} | {md_cell(by[(team, what)])} |" for team, what in sorted(tests)[:40]]
        lines.append("")
    lines += ["### How this was computed", "",
              f"- Teams: CODEOWNERS ({m['codeowners_rules']} rules), else a `team/` label, else the top-level directory. {m['note']}",
              "- Blast radius: changed lines, modules, auth/billing/security/SRE areas (always high), contract/public changes (external), and other teams' code that uses the changed symbols (internal).",
              f"- Breaking changes: diff heuristics for the top {m['diff_limit']} PRs by priority ({m['diff_prs']} read, {len(m['diff_errors'])} failed to fetch), path-only candidates for the rest, declared breaking changes in titles, bodies and labels."
              + (f" {m['files_truncated_prs']} PR(s) have more files than the tracker returned." if m["files_truncated_prs"] else ""), ""]
    return "\n".join(lines)


def digest_line(out: dict) -> str:
    """One Slack line ("" when there is nothing to say)."""
    s = out["summary"]
    if not out["features"]:
        return ""
    n = s["breaking_high"] + s["breaking_medium"]
    top = out["breaking_changes"][0] if out["breaking_changes"] else None
    return (f"{s['features']} change(s) · {n} breaking-change candidate{'s' if n != 1 else ''} ({s['breaking_high']} high confidence)"
            + (f" · first: {top['kind']} in PR #{top['pr']} ({', '.join(top['teams'][:2])})" if top else ""))


def write_outputs(out: dict, reports_dir: Path, label: str) -> str:
    """changelog.json + changelog.{md,html}; returns the markdown section for the main report."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "changelog.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    md = render_markdown(out)
    write_markdown_report(reports_dir, "changelog", f"Changelog & blast radius -- {label}", md)
    return md
