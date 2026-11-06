# SPDX-License-Identifier: LGPL-2.1-or-later
"""The audit's changelog, breaking-change candidates and blast radius: merge the model's judgement with deterministic facts.

The model (specialist ``changelog-blast-radius``) writes ``reports/changelog_model.json``: themes with a one-line summary per
PR, plain-language breaking-change descriptions and test ideas. Owners, blast radius and signals come from ``change_impact``
and always win: the model cannot lower a blast radius or remove a signal. Every PR the model did not place is still listed
(grouped by owning team, typed from its title), and without a usable model file the whole changelog is built that way, so
the report always shows what changed.

Two documents come out of one merge: ``changelog.{md,html}`` (what changed, what may break) and ``blast_radius.{md,html}``
(who is affected, which tests to run); the audit report carries both as one section.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.common.report_bundle import md_cell
from scripts.common.report_renderer import write_markdown_report

_CONF = ("high", "medium", "low")
_BLAST = ("low", "medium", "high")
TYPES = {"feature": "New", "improvement": "Improved", "fix": "Fixed", "security": "Security", "platform": "Platform", "change": "Changed"}
_PREFIX = re.compile(r"^\s*(?:\[?[A-Z][A-Z0-9]+-\d+\]?\s*[:\-]?\s*)?([A-Za-z]+)(?:\([^)]*\))?!?\s*:\s*")
_TICKET = re.compile(r"^\s*\[?[A-Z][A-Z0-9]+-\d+\]?\s*[:\-]?\s*")
_PREFIX_TYPE = {"feat": "feature", "feature": "feature", "add": "feature", "fix": "fix", "bugfix": "fix", "hotfix": "fix", "perf": "improvement",
                "refactor": "improvement", "improve": "improvement", "security": "security", "sec": "security", "revert": "change",
                "chore": "maintenance", "docs": "maintenance", "doc": "maintenance", "test": "maintenance", "tests": "maintenance", "ci": "maintenance",
                "build": "maintenance", "style": "maintenance", "deps": "maintenance", "dep": "maintenance", "bump": "maintenance", "lint": "maintenance"}
_MAINTENANCE = re.compile(r"(?i)\b(bump|dependabot|renovate|dependenc|lockfile|typo|flaky|fix(?:es)? tests?|update tests?|add tests?)\b")
_FIXISH = re.compile(r"(?i)^\s*(?:fix|fixes|fixed|hotfix|bugfix|resolve[sd]?|correct)\b|\b(bug|regression|crash|npe|leak)\b")
_SECURITY = re.compile(r"(?i)\b(cve-\d|vulnerab|security|xss|csrf|injection|authz|rbac|secret)\b")
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

_WHAT = {
    "field_removed": "A field was removed from the contract ({e}); clients that still read it break",
    "field_renamed": "A field was renamed ({e}); clients using the old name break",
    "field_renumbered": "A wire field number changed ({e}); payloads from the previous release decode wrongly",
    "field_type_changed": "A field changed type ({e}); clients parsing the old type break",
    "enum_value_removed": "An enum value was removed ({e}); stored or sent values of it no longer map",
    "enum_value_changed": "An enum value changed ({e}); persisted and wire values no longer match",
    "rpc_removed": "An RPC was removed ({e}); callers fail",
    "type_removed": "A type was removed from the contract ({e}); consumers no longer build",
    "endpoint_removed": "An endpoint was removed ({e}); callers get errors",
    "route_removed": "A route was removed ({e}); callers get errors",
    "route_method_changed": "A route changed its HTTP method ({e}); callers using the old method fail",
    "required_added": "New required input ({e}); existing requests or configs without it are rejected",
    "schema_field_removed": "A schema property was removed ({e}); readers of it break",
    "type_changed": "A schema type changed ({e}); producers or consumers of the old type break",
    "status_code_changed": "An HTTP status code changed ({e}); clients branching on the old code misbehave",
    "required_param_added": "A function gained a required parameter ({e}); existing callers no longer compile or run",
    "param_removed": "A function parameter was removed ({e}); existing callers break",
    "public_symbol_removed": "A public symbol was removed ({e}); dependents no longer build",
    "db_drop": "A database object is dropped ({e}); anything reading it fails and the data is gone",
    "db_rename": "A database column or table was renamed ({e}); the previous release cannot run against the new schema",
    "db_type_changed": "A database column changed type ({e}); old readers and writers may fail, the migration can lock or rewrite the table",
    "db_not_null_no_default": "A NOT NULL column without a default was added ({e}); older writers that omit it fail",
    "db_not_null_added": "NOT NULL was enforced ({e}); rows or writers with nulls fail",
    "db_constraint_added": "A database constraint was added ({e}); existing data or writers may violate it",
    "default_changed": "A default, limit or timeout changed ({e}); behaviour changes for everyone relying on the old value",
    "declared_breaking": "The PR declares a breaking change ({e})",
    "contract_changed": "A contract file changed ({e}); not yet checked for compatibility",
    "migration_changed": "A migration changed ({e}); rollout and rollback need a rehearsal",
    "config_changed": "Configuration keys or defaults changed ({e})",
    "api_changed": "API handlers changed ({e}); not yet checked for compatibility",
    "enum_or_model_changed": "A model or enum changed ({e}); serialization and switches may break",
    "deprecation": "Something is deprecated ({e}); callers must migrate before removal",
    "semantic": "Behaviour changed while the contract stayed the same ({e})",
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
    if not isinstance(data, dict) or not isinstance(data.get("themes"), list):
        return None
    themes = []
    for t in data["themes"]:
        if not isinstance(t, dict) or not t.get("title"):
            continue
        items = []
        for i in t.get("items") or []:
            pr = (_ints(i.get("pr")) or [0])[0] if isinstance(i, dict) else 0
            if pr:
                items.append({"pr": pr, "type": i["type"] if i.get("type") in TYPES else "", "summary": str(i.get("summary") or "").strip(),
                              "verify": str(i.get("verify") or "").strip()})
        themes.append({"title": str(t["title"]).strip(), "summary": str(t.get("summary") or "").strip(), "items": items})
    breaking = [{"pr": (_ints(b.get("pr")) or [0])[0], "kind": str(b.get("kind") or "semantic"), "confidence": b["confidence"] if b.get("confidence") in _CONF else "low",
                 "summary": str(b.get("summary") or "").strip(), "evidence": str(b.get("evidence") or ""), "consumers": _strs(b.get("consumers")),
                 "migration": str(b.get("migration") or "")} for b in data.get("breaking") or [] if isinstance(b, dict)]
    tests = [{"team": str(t["team"]), "what": str(t["what"])} for t in data.get("tests") or [] if isinstance(t, dict) and t.get("team") and t.get("what")]
    return {"themes": themes, "breaking": breaking, "tests": tests}


def load_model(reports_dir: Path) -> dict | None:
    """``reports/changelog_model.json`` coerced; None when absent, malformed or without themes."""
    try:
        return _coerce(json.loads((reports_dir / "changelog_model.json").read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def classify_change(title: str, labels: list[str] | None = None) -> str:
    """feature | improvement | fix | security | platform | change | maintenance, from a conventional-commit prefix, labels and wording."""
    m = _PREFIX.match(title or "")
    if m and m.group(1).lower() in _PREFIX_TYPE:
        return _PREFIX_TYPE[m.group(1).lower()]
    text = f"{title} {' '.join(labels or [])}"
    if _SECURITY.search(text):
        return "security"
    if _MAINTENANCE.search(text):
        return "maintenance"
    return "fix" if _FIXISH.search(text) else "change"


def plain_title(title: str) -> str:
    """The PR title without its conventional-commit prefix and ticket key."""
    t = _PREFIX.sub("", title or "", count=1) if (m := _PREFIX.match(title or "")) and m.group(1).lower() in _PREFIX_TYPE else _TICKET.sub("", title or "", count=1)
    return t.strip() or (title or "").strip()


def _pr(n: int, url: str = "") -> str:
    return f"[#{n}]({url})" if url else f"#{n}"


def what_broke(kind: str, evidence: str, file: str = "") -> str:
    """One plain-language sentence for a breaking-change signal."""
    e = " ".join(evidence.split()) or file.rsplit("/", 1)[-1]   # escaping happens once, where the sentence is rendered
    return _WHAT.get(kind, "Possible incompatible change ({e})").format(e=e[:110])


def _breaking_rows(facts: dict, model: dict | None) -> list[dict]:
    """Breaking-change candidates, one row per (PR, kind, file) with the evidence of repeated hits collapsed into one sentence."""
    by_num = {r["number"]: r for r in facts["prs"]}
    groups: dict[tuple, dict] = {}
    for r in facts["prs"]:
        for s in r["signals"]:
            if s["confidence"] == "low" and s["kind"] != "declared_breaking":
                continue
            g = groups.setdefault((r["number"], s["kind"], s["file"]), {"pr": r["number"], "kind": s["kind"], "file": s["file"], "confidence": s["confidence"],
                                                                    "evidence": [], "consumers": r["blast"]["internal"], "migration": "", "summary": "", "source": "signal"})
            g["confidence"] = min(g["confidence"], s["confidence"], key=_CONF.index)
            if s["evidence"] not in g["evidence"]:
                g["evidence"].append(s["evidence"])
    rows = list(groups.values())
    for b in (model or {}).get("breaking", []):
        if b["pr"] not in by_num:
            continue
        match = next((g for g in rows if g["pr"] == b["pr"] and g["kind"] == b["kind"]), None)
        if match:
            match["summary"], match["migration"] = b["summary"] or match["summary"], b["migration"] or match["migration"]
            match["consumers"] = b["consumers"] or match["consumers"]
        else:
            rows.append({"pr": b["pr"], "kind": b["kind"], "file": "", "confidence": b["confidence"], "evidence": [b["evidence"]] if b["evidence"] else [],
                         "consumers": b["consumers"] or by_num[b["pr"]]["blast"]["internal"], "migration": b["migration"], "summary": b["summary"], "source": "model"})
    out = []
    for g in rows:
        r = by_num[g["pr"]]
        ev = "; ".join(g["evidence"][:4]) + (f" (+{len(g['evidence']) - 4} more)" if len(g["evidence"]) > 4 else "")
        out.append({"pr": g["pr"], "title": r["title"], "url": r["url"], "confidence": g["confidence"], "kind": g["kind"], "file": g["file"],
                    "what": g["summary"] or what_broke(g["kind"], ev, g["file"]), "evidence": ev, "consumers": g["consumers"], "teams": r["teams"][:2],
                    "migration": g["migration"] or _TEST_HINTS.get(g["kind"], "")})
    out.sort(key=lambda b: (_CONF.index(b["confidence"]), -b["pr"]))
    return out


def _themes(facts: dict, model: dict | None) -> tuple[list[dict], list[dict]]:
    """``(themes, maintenance)``: the model's themes first, then every other PR grouped by owning team; chores are only counted."""
    by_num = {r["number"]: r for r in facts["prs"]}
    placed: set[int] = set()

    def item(r: dict, typ: str = "", summary: str = "", verify: str = "") -> dict:
        return {"pr": r["number"], "url": r["url"], "title": r["title"], "type": typ or classify_change(r["title"], r.get("labels")),
                "summary": summary or plain_title(r["title"]), "verify": verify, "team": (r["teams"] or [""])[0], "blast": r["blast"]["level"]}

    themes = []
    for t in (model or {}).get("themes", []):
        items = []
        for i in t["items"]:
            if i["pr"] in by_num and i["pr"] not in placed:
                placed.add(i["pr"])
                items.append(item(by_num[i["pr"]], i["type"], i["summary"], i["verify"]))
        if items:
            themes.append({"title": t["title"], "summary": t["summary"], "items": items, "source": "model"})
    rest: dict[str, list[dict]] = {}
    maintenance = []
    for r in facts["prs"]:
        if r["number"] in placed:
            continue
        it = item(r)
        if it["type"] == "maintenance":   # decided by title and labels: a bot-authored PR can still be a real change
            maintenance.append({"pr": r["number"], "url": r["url"], "title": r["title"]})
        else:
            rest.setdefault(it["team"] or "Other", []).append(it)
    label = "More changes" if themes else "Changes"
    for team, items in sorted(rest.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        themes.append({"title": f"{label}: {team}", "summary": "", "items": sorted(items, key=lambda i: -i["pr"]), "source": "facts"})
    return themes, maintenance


def merge(facts: dict, model: dict | None) -> dict:
    model = _coerce(model) if model is not None else None
    themes, maintenance = _themes(facts, model)
    breaking = _breaking_rows(facts, model)
    summaries = {i["pr"]: i for t in themes for i in t["items"]}
    chores = {m["pr"] for m in maintenance}
    summaries.update({m["pr"]: {"summary": plain_title(m["title"])} for m in maintenance})
    teams = {}
    for t, st in facts["teams"].items():   # a team's notable changes: real changes before chores, then by blast radius
        owned = sorted((r for r in facts["prs"] if r["teams"][:1] == [t] and r["number"] in summaries),
                       key=lambda r: (r["number"] in chores, -_BLAST.index(r["blast"]["level"]), -r["number"]))
        teams[t] = {**st, "changes": [{"pr": r["number"], "url": r["url"], "summary": summaries[r["number"]]["summary"], "blast": r["blast"]["level"]} for r in owned[:3]]}
    tests = list(model["tests"]) if model else []
    for b in breaking:
        for team in b["consumers"] or b["teams"]:
            test = {"team": team, "what": _TEST_HINTS.get(b["kind"], ""), "pr": b["pr"]}
            if test["what"] and not any(x["team"] == team and x["what"] == test["what"] for x in tests):
                tests.append(test)
    changes = sum(len(t["items"]) for t in themes)
    return {"mode": "model" if model is not None else "facts", "themes": themes, "maintenance": maintenance, "breaking_changes": breaking, "teams": teams,
            "tests": tests[:40], "meta": facts["meta"],
            "summary": {"changes": changes, "themes": len(themes), "maintenance": len(maintenance), "teams": len(teams),
                        "breaking_high": sum(b["confidence"] == "high" for b in breaking), "breaking_medium": sum(b["confidence"] == "medium" for b in breaking),
                        "candidates_low": sum(1 for r in facts["prs"] for s in r["signals"] if s["confidence"] == "low")}}


def _changelog_md(out: dict, heading: str) -> str:
    s, m = out["summary"], out["meta"]
    lines = [heading, "", f"*{s['changes']} change(s) in {s['themes']} group(s)" + (f" and {s['maintenance']} maintenance PR(s)" if s["maintenance"] else "")
             + f" across {m['prs']} PRs.*", ""]
    if not out["themes"] and not out["maintenance"]:
        return "\n".join(lines + ["No merged PRs in this audit.", ""])
    for t in out["themes"]:
        lines += [f"#### {md_cell(t['title'])}", ""]
        lines += [md_cell(t["summary"]), ""] if t["summary"] else []
        for i in t["items"]:
            extra = " · ".join(x for x in (md_cell(i["team"]), f"blast {i['blast']}" if i["blast"] != "low" else "") if x)
            lines.append(f"- **{TYPES.get(i['type'], 'Changed')}**: {md_cell(i['summary'])} ({_pr(i['pr'], i['url'])})" + (f" · {extra}" if extra else ""))
            lines += [f"  - Verify: {md_cell(i['verify'])}"] if i["verify"] else []
        lines.append("")
    if out["maintenance"]:
        links = ", ".join(_pr(x["pr"], x["url"]) for x in out["maintenance"][:60])
        lines += ["#### Maintenance", "", f"Dependency bumps, tests, docs, CI and other chores: {links}" + (f" and {len(out['maintenance']) - 60} more" if len(out["maintenance"]) > 60 else ""), ""]
    lines += ["### Breaking-change candidates", ""]
    if not out["breaking_changes"]:
        return "\n".join(lines + ["None found: no declared breaking change and no medium or high confidence signal in the PRs examined.", ""])
    lines += ["| Confidence | What may break | PR | Affected | What to do |", "| --- | --- | --- | --- | --- |"]
    for b in out["breaking_changes"][:40]:
        pr = f"{_pr(b['pr'], b['url'])} {md_cell(plain_title(b['title']))[:70]}"
        lines.append(f"| {b['confidence']} | {md_cell(b['what'])[:260]} | {pr} | {md_cell(', '.join(b['consumers'] or b['teams']) or 'unknown')} | {md_cell(b['migration'])[:200]} |")
    lines += ["", f"*{len(out['breaking_changes'])} candidate(s). They are a review aid, not a gate: heuristics over the diffs read plus a model's judgement.*", ""]
    return "\n".join(lines)


def _blast_md(out: dict, heading: str) -> str:
    m = out["meta"]
    lines = [heading, "", "| Team | PRs | High blast | Breaking candidates | Notable changes |", "| --- | --- | --- | --- | --- |"]
    for t, st in sorted(out["teams"].items(), key=lambda kv: (-kv[1]["high_blast"], -kv[1]["prs"], kv[0])):
        notable = "; ".join(f"{_pr(c['pr'], c['url'])} {md_cell(c['summary'])[:80]}" for c in st["changes"])
        lines.append(f"| {md_cell(t)} | {st['prs']} | {st['high_blast']} | {st['breaking_candidates']} | {notable} |")
    if out["tests"]:
        lines += ["", "### Suggested tests", "", "| Team | Test |", "| --- | --- |"]
        lines += [f"| {md_cell(t['team'])} | {md_cell(t['what'])} |" for t in out["tests"]]
    lines += ["", "### How this was computed", "",
              f"- Teams: CODEOWNERS ({m['codeowners_rules']} rules), else a `team/` label, else the top-level directory; a PR counts for the team owning most of its files. {m['note']}",
              "- Blast radius: changed lines, modules, auth/billing/security/SRE areas (always high), contract/public changes (external), and other teams' code that uses the changed symbols (internal).",
              f"- Breaking changes: diff heuristics for the top {m['diff_limit']} PRs by priority ({m['diff_prs']} read, {len(m['diff_errors'])} failed to fetch), path-only candidates for the rest, declared breaking changes in titles, bodies and labels."
              + (f" {m['files_truncated_prs']} PR(s) have more files than the tracker returned." if m["files_truncated_prs"] else ""), ""]
    return "\n".join(lines)


def render_markdown(out: dict, heading: str = "## Changelog & Blast Radius") -> str:
    """Both documents as one section (the audit report)."""
    return f"{heading}\n\n{_changelog_md(out, '### Changelog')}\n\n{_blast_md(out, '### Blast radius by team')}"


def digest_line(out: dict) -> str:
    """One Slack line ("" when there is nothing to say)."""
    s = out["summary"]
    if not (out["themes"] or out["maintenance"]):
        return ""
    n = s["breaking_high"] + s["breaking_medium"]
    top = out["breaking_changes"][0] if out["breaking_changes"] else None
    return (f"{s['changes']} change(s) in {s['themes']} group(s) · {n} breaking-change candidate{'s' if n != 1 else ''} ({s['breaking_high']} high confidence)"
            + (f" · first: {top['what'][:90]} (PR #{top['pr']})" if top else ""))


def write_outputs(out: dict, reports_dir: Path, label: str) -> str:
    """changelog.json, changelog.{md,html} and blast_radius.{md,html}; returns the combined markdown section for the main report."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "changelog.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    write_markdown_report(reports_dir, "changelog", f"Changelog -- {label}", _changelog_md(out, "## Changelog"))
    write_markdown_report(reports_dir, "blast_radius", f"Blast radius -- {label}", _blast_md(out, "## Blast radius by team"))
    return render_markdown(out)
