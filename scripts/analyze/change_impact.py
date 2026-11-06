# SPDX-License-Identifier: LGPL-2.1-or-later
"""Deterministic facts for the audit's changelog: who owns a change, how far it reaches, what may break and for whom.

Everything here is computed from PR data, CODEOWNERS and (for a capped number of risky PRs) diffs. The model only turns
the facts into a readable changelog and judges semantic changes; owners, blast radius and signals are never model output.

  * teams     CODEOWNERS of the changed files (by files touched), else a ``team/x`` label, else the top-level directory.
  * blast     one rule (``pr_classify.blast_level``) plus floors: auth/billing/security/SRE-infra areas are always high,
              a contract/public/API change is at least medium and external, and consumers found in other teams' code
              (bounded ``git grep`` for the changed symbols) are the internal blast radius.
  * signals   ``breaking_changes``: declared breaking changes, diff heuristics for the PRs whose diff was read
              (top ``diff_limit`` by priority), path-only candidates for the rest.
"""

from __future__ import annotations

import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from scripts.analyze import breaking_changes as bc
from scripts.common import change_classes as cc
from scripts.common import codeowners as co
from scripts.common.pr_classify import SENSITIVE_PATHS, blast_level, is_test_file, top_level_module
from scripts.common.pr_context import fetch_pr_diff

MAX_TEAMS = 3                # a PR touching many areas is owned by its biggest few; the rest would only add noise to every table
MAX_SYMBOLS = 6
MAX_GREP_HITS = 40
CONSUMER_SCAN_SECONDS = 90   # total time budget for the consumer search across all PRs; the rest is reported as not scanned
DIFF_BYTES = 150_000
_GENERIC = {"index", "main", "types", "type", "utils", "util", "models", "model", "api", "apis", "config", "test", "tests", "common", "base", "init",
            "mod", "lib", "src", "app", "core", "handler", "handlers", "route", "routes", "client", "server", "schema", "schemas", "enum", "enums",
            "constants", "constant", "status", "state", "settings", "default", "defaults", "readme", "package", "setup", "version"}
_TYPE_WORDS = {"string", "int32", "int64", "uint32", "uint64", "bool", "bytes", "float", "double", "message", "enum", "rpc", "property", "required",
               "path", "type", "value", "repeated", "optional", "enum_value", "field", "param", "parameter", "def", "func", "function"}
_CONF_RANK = {"high": 0, "medium": 1, "low": 2}
_BLAST_RANK = {"high": 0, "medium": 1, "low": 2}
_DEPENDENCY_PR = re.compile(r"(?i)^(?:chore|build|fix|deps?)?(?:\(deps?[^)]*\))?[:!]?\s*(?:bump|update|upgrade)\b")
_TEAM_LABEL = re.compile(r"(?i)^(?:team|squad|owner)[:/ \-]+(.+)$")


def _git_grep(symbol: str, repo_dir: str) -> list[str]:
    """Files in the checkout that mention ``symbol`` as a whole word (bounded; [] when there is no checkout)."""
    if not repo_dir:
        return []
    try:
        out = subprocess.run(["git", "-C", repo_dir, "grep", "-l", "-I", "-w", "-F", "-e", symbol, "--", "."], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return out.stdout.splitlines()[:MAX_GREP_HITS]


def _teams(paths: list[tuple[str, int]], labels: list[str], rules: list[co.Rule]) -> tuple[list[str], list[str], str]:
    """(teams, raw owners, source) for the changed paths: the ``MAX_TEAMS`` owning the most files first."""
    weight: dict[str, int] = {}
    raw: dict[str, int] = {}
    for path, size in paths:
        for owner in co.owners_for(path, rules):
            weight[co.team_name(owner)] = weight.get(co.team_name(owner), 0) + 1
            raw[owner] = raw.get(owner, 0) + size
    if weight:
        return sorted(weight, key=lambda t: (-weight[t], t))[:MAX_TEAMS], sorted(raw), "codeowners"
    for label in labels:
        if m := _TEAM_LABEL.match(label):
            return [m.group(1).strip()], [], "label"
    biggest = max(paths, key=lambda p: p[1], default=("", 0))[0]
    return ([f"area:{top_level_module(biggest)}"] if biggest else []), [], "area"


def _symbols(classes: dict[str, list[str]], signals: list[dict]) -> list[str]:
    stems = [p.rsplit("/", 1)[-1].split(".")[0] for c in ("contract", "model", "enum", "api", "public_surface") for p in classes.get(c, [])]
    names = []
    for s in signals:
        if s["confidence"] in ("high", "medium") and s["kind"] not in ("declared_breaking", "deprecation"):
            names += [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", s["evidence"]) if t.lower() not in _TYPE_WORDS][:1]
    chosen: list[str] = []
    for sym in [*dict.fromkeys(stems)][:3] + [*dict.fromkeys(names)][:3]:
        if len(sym) >= 3 and sym.lower() not in _GENERIC and sym not in chosen:
            chosen.append(sym)
    return chosen[:MAX_SYMBOLS]


def _consumers(symbols: list[str], own_files: set[str], own_teams: set[str], rules: list[co.Rule], repo_dir: str, search) -> list[dict]:
    found: dict[tuple[str, str], list[str]] = {}
    for sym in symbols:
        for path in search(sym):
            if path in own_files or is_test_file(path) or re.search(r"(?i)\.(md|rst|txt)$|(^|/)(docs?|tests?)/", path):
                continue
            owners = co.owners_for(path, rules)
            for team in ({co.team_name(o) for o in owners} or {f"area:{top_level_module(path)}"}) - own_teams:
                found.setdefault((team, sym), []).append(path)
    return [{"team": t, "symbol": s, "paths": p[:3]} for (t, s), p in sorted(found.items())]


def build_facts(prs: list[dict], rules: list[co.Rule], config: dict, diff_limit: int = 25, repo_dir: str = "") -> dict:
    """``{prs, teams, breaking, meta}``; see the module docstring."""
    tracker = "bitbucket" if (config.get("DEFAULT_TRACKER") or "").lower() in ("jira", "jira/bitbucket", "bitbucket") else "github"

    records: dict[int, dict] = {}
    for pr in prs:
        changes = pr.get("file_changes") or [{"path": p, "additions": 0, "deletions": 0} for p in pr.get("file_paths", [])]
        paths = [c["path"] for c in changes if c.get("path")]
        classes = cc.classify_files(paths)
        loc = sum(c.get("additions", 0) + c.get("deletions", 0) for c in changes)
        areas = sorted({a for p in paths if (a := cc.sensitive_area(p))})
        teams, owners, source = _teams([(c["path"], c.get("additions", 0) + c.get("deletions", 0)) for c in changes if c.get("path")], pr.get("labels") or [], rules)
        modules = {top_level_module(p) for p in paths}
        level = blast_level(loc, len(modules), any(SENSITIVE_PATHS.search(p) for p in paths))
        reasons = []
        if areas:
            reasons.append(f"touches the {', '.join(areas)} area")
        if loc > 300:
            reasons.append(f"{loc} changed lines")
        if len(modules) >= 3:
            reasons.append(f"{len(modules)} modules")
        if set(areas) & set(cc.HIGH_BLAST_AREAS) or "infra" in classes and "sre" in areas:
            level = "high"
        external = cc.is_external(classes)
        if external:
            reasons.append("changes a contract, API or public surface (external consumers)")
            level = level if level != "low" else "medium"
        title = pr.get("title", "")
        automated = bool(pr.get("is_bot_authored")) or bool(_DEPENDENCY_PR.search(title)) or any(re.search(r"(?i)dependenc", lb) for lb in pr.get("labels") or [])
        declared = bc.analyze_text(title, pr.get("body", ""), pr.get("labels") or [], trust_body=not automated)
        records[pr["number"]] = {
            "number": pr["number"], "title": pr.get("title", ""), "url": pr.get("url", ""), "author": pr.get("author", ""), "state": pr.get("state", ""),
            "merged_at": pr.get("merged_at", ""), "files": len(paths), "loc": loc, "files_truncated": bool(pr.get("files_truncated")),
            "labels": pr.get("labels") or [], "classes": {c: v[:10] for c, v in classes.items()}, "areas": areas, "teams": teams, "owners": owners, "owner_source": source,
            "blast": {"level": level, "external": external, "internal": [], "reasons": reasons}, "signals": declared, "consumers": [], "diff_read": False,
            "_declared": bool(any(s["kind"] == "declared_breaking" for s in declared)), "_paths": set(paths),
        }

    ranked = sorted(((bc.priority(r["classes"], r["_declared"], r["blast"]["level"]), -r["number"]) for r in records.values()), reverse=True)
    chosen = [-n for score, n in ranked if score > 0][:max(diff_limit, 0)]
    errors: list[dict] = []

    def read(number: int):
        return number, fetch_pr_diff(config, tracker, str(number), DIFF_BYTES)

    deadline = time.monotonic() + CONSUMER_SCAN_SECONDS
    grep_cache: dict[str, list[str]] = {}
    skipped_scans = 0

    def search(symbol: str) -> list[str]:
        """Cached, time-boxed ``git grep``: the same symbol is searched once per audit, and none after the budget is spent."""
        nonlocal skipped_scans
        if symbol not in grep_cache:
            if time.monotonic() > deadline:
                skipped_scans += 1
                return []
            grep_cache[symbol] = _git_grep(symbol, repo_dir)
        return grep_cache[symbol]

    if chosen:
        with ThreadPoolExecutor(max_workers=min(8, len(chosen))) as pool:
            for number, (diff, err) in pool.map(read, chosen):
                if err or not diff:
                    errors.append({"pr": number, "error": err or "empty diff"})
                    continue
                records[number]["diff_read"] = True
                records[number]["signals"] += bc.analyze_diff(diff)
    for r in records.values():
        if not r["diff_read"]:
            r["signals"] += bc.tier1(r["classes"])
        r["signals"].sort(key=lambda s: _CONF_RANK[s["confidence"]])
        if r["classes"].keys() & {"contract", "model", "enum", "api", "public_surface"}:
            r["consumers"] = _consumers(_symbols(r["classes"], r["signals"]), r["_paths"], set(r["teams"]), rules, repo_dir, search)
            r["blast"]["internal"] = sorted({c["team"] for c in r["consumers"]})
            if r["blast"]["internal"]:
                r["blast"]["reasons"].append(f"used by {', '.join(r['blast']['internal'])}")
                if r["blast"]["level"] == "low":
                    r["blast"]["level"] = "medium"
        del r["_declared"], r["_paths"]

    teams: dict[str, dict] = {}
    for r in records.values():
        for t in r["teams"][:1]:   # a PR counts once, for the team owning most of it
            s = teams.setdefault(t, {"prs": 0, "high_blast": 0, "breaking_candidates": 0})
            s["prs"] += 1
            s["high_blast"] += r["blast"]["level"] == "high"
            s["breaking_candidates"] += any(x["confidence"] in ("high", "medium") for x in r["signals"])
    breaking = sorted(({"pr": r["number"], "teams": r["teams"], "consumers": r["blast"]["internal"], **s} for r in records.values() for s in r["signals"]),
                      key=lambda b: (_CONF_RANK[b["confidence"]], -b["pr"]))
    ordered = sorted(records.values(), key=lambda r: -r["number"])
    return {"prs": ordered, "teams": teams, "breaking": breaking,
            "meta": {"prs": len(ordered), "diff_prs": sum(r["diff_read"] for r in ordered), "diff_limit": diff_limit, "diff_errors": errors,
                     "codeowners_rules": len(rules), "files_truncated_prs": sum(r["files_truncated"] for r in ordered), "consumer_searches_skipped": skipped_scans,
                     "note": "Ownership and consumers come from the checked-out branch, not from the merge commit."}}


def _inline(text: str, limit: int = 120) -> str:
    """Untrusted text made safe for the prompt's fenced facts block: one line, no fence characters."""
    return re.sub(r"\s+", " ", (text or "").replace("`", "'"))[:limit]


def prompt_facts(facts: dict, max_lines: int = 600) -> str:
    """One compact line per PR (risky ones first) for the model, capped at ``max_lines``: the changelog covers every PR."""
    lines = []
    for r in sorted(facts["prs"], key=lambda r: (_BLAST_RANK[r["blast"]["level"]], -len(r["signals"]), -r["number"])):
        sig = ", ".join(f"{s['kind']}({s['confidence']})" for s in r["signals"] if s["confidence"] != "low")[:100]
        cons = ", ".join(sorted({c["team"] for c in r["consumers"]}))
        lines.append(f"#{r['number']} [{r['blast']['level']}{' external' if r['blast']['external'] else ''}] {','.join(r['teams'][:2]) or '?'} | "
                     f"{_inline(r['title'], 100)}" + (f" | signals: {sig}" if sig else "") + (f" | used by: {cons}" if cons else ""))
    teams = "; ".join(f"{t} ({s['prs']} PRs, {s['high_blast']} high blast, {s['breaking_candidates']} breaking candidates)" for t, s in sorted(facts["teams"].items(), key=lambda kv: -kv[1]["prs"])[:15])
    return f"Teams: {teams or 'unknown'}\n" + "\n".join(lines[:max_lines])
