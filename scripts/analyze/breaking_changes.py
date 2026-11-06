# SPDX-License-Identifier: LGPL-2.1-or-later
"""Backward-incompatibility signals for a PR: deterministic heuristics over diffs, paths and PR text.

Not a verdict. Every signal carries ``confidence`` (high / medium / low) and ``evidence``; the audit's changelog model
judges them (and semantic changes the heuristics cannot see) and the report calls them candidates.

  * ``analyze_diff``  signals from a unified diff: removed/renumbered/retyped proto fields, removed enum values and rpcs,
                      removed schema properties/endpoints, new required properties, removed or re-pathed routes, changed
                      status codes, removed or re-signatured public functions, destructive or locking migrations, and
                      changed defaults/limits/timeouts (a semantic change with an unchanged contract).
  * ``analyze_text``  breaking changes declared in the title, body or labels.
  * ``tier1``         path-only candidates for PRs whose diff was not read.
  * ``priority``      which PRs deserve a diff read when diffs are capped.
Kinds and the tests that fit them: ``skills/shared/breaking-change-signals.md`` (you-got-skills).
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.common import change_classes as cc
from scripts.common.pr_classify import is_test_file
from scripts.common.redact import redact

MAX_SIGNALS_PER_FILE = 20
MAX_LINE_CHARS = 500   # longer diff lines (minified or generated code) are cut: the heuristics never need them whole
_CODE_EXT = {".py", ".go", ".js", ".ts", ".tsx", ".java", ".kt", ".rb", ".rs", ".cs", ".php", ".scala"}
_MANIFEST = re.compile(r"(?i)(^|/)(go\.(mod|sum)|package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.(toml|lock)|requirements[^/]*\.txt|Gemfile(\.lock)?|[^/]+\.lock)$")
_FILE_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def _sig(kind: str, file: str, evidence: str, confidence: str, detail: str = "") -> dict:
    # Evidence is a quoted diff line: it can contain a credential that sits next to a "default" or "limit" key.
    return {"kind": kind, "file": file, "evidence": redact(evidence.strip()[:200]), "confidence": confidence, "detail": redact(detail)}


def parse_diff(diff: str) -> dict[str, dict]:
    """``{path: {removed, added, lines}}``; ``lines`` keeps context too, as ``(sign, text)``, in order."""
    files: dict[str, dict] = {}
    cur: dict | None = None
    for raw in (diff or "").splitlines():
        m = _FILE_HEADER.match(raw)
        if m:
            cur = files.setdefault(m.group(2), {"removed": [], "added": [], "lines": []})
            continue
        if cur is None or raw.startswith(("--- ", "+++ ", "@@", "index ", "new file", "deleted file", "similarity", "rename ", "Binary files", "\\ No newline")):
            continue
        if raw[:1] in ("-", "+"):
            cur["removed" if raw[0] == "-" else "added"].append(raw[1:MAX_LINE_CHARS])
            cur["lines"].append((raw[0], raw[1:MAX_LINE_CHARS]))
        elif raw[:1] == " ":
            cur["lines"].append((" ", raw[1:MAX_LINE_CHARS]))
    return files


# -- proto ----------------------------------------------------------------------------------------------------
_P_FIELD = re.compile(r"^\s*(?:optional\s+|repeated\s+|required\s+)?([\w.<>, ]+?)\s+(\w+)\s*=\s*(\d+)\b[^;]*;")
_P_ENUMV = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(-?\d+)\s*(?:\[.*\])?;")
_P_RPC = re.compile(r"^\s*rpc\s+(\w+)\s*\(")
_P_TYPE = re.compile(r"^\s*(message|service|enum)\s+(\w+)")


def _proto(path: str, d: dict) -> list[dict]:
    out: list[dict] = []

    def collect(lines):
        fields, enums, rpcs, types = {}, {}, set(), {}
        for ln in lines:
            if m := _P_ENUMV.match(ln):
                enums[m.group(1)] = m.group(2)
            elif m := _P_FIELD.match(ln):
                fields[m.group(2)] = (m.group(1).strip(), m.group(3))
            elif m := _P_RPC.match(ln):
                rpcs.add(m.group(1))
            elif m := _P_TYPE.match(ln):
                types[m.group(2)] = m.group(1)
        return fields, enums, rpcs, types

    rf, re_, rr, rt = collect(d["removed"])
    af, ae, ar, at = collect(d["added"])
    added_numbers = {n: name for name, (_, n) in af.items()}
    for name, (typ, num) in rf.items():
        if name in af:
            if af[name][0] != typ:
                out.append(_sig("field_type_changed", path, f"{name}: {typ} -> {af[name][0]}", "high"))
            elif af[name][1] != num:
                out.append(_sig("field_renumbered", path, f"{name}: {num} -> {af[name][1]}", "high"))
        elif num in added_numbers:
            out.append(_sig("field_renamed", path, f"{name} -> {added_numbers[num]} (field {num})", "high"))
        else:
            out.append(_sig("field_removed", path, f"{typ} {name} = {num}", "high"))
    for name, val in re_.items():
        if name in ae:
            if ae[name] != val:
                out.append(_sig("enum_value_changed", path, f"{name}: {val} -> {ae[name]}", "high"))
        else:
            out.append(_sig("enum_value_removed", path, f"{name} = {val}", "high"))
    out += [_sig("rpc_removed", path, f"rpc {n}", "high") for n in sorted(rr - ar)]
    out += [_sig("type_removed", path, f"{kind} {n}", "high") for n, kind in rt.items() if n not in at]
    return out


# -- JSON / YAML schemas ----------------------------------------------------------------------------------------
_S_KEY = re.compile(r"^(\s*)([\w$./{}-]+):\s*$")
_S_ITEM = re.compile(r"^(\s*)-\s+(.+?)\s*$")
_S_PATH = re.compile(r"""^\s*["']?(/[^\s:"']+)["']?:\s*(?:\{)?\s*$""")
_S_TYPE = re.compile(r"""^\s*["']?type["']?\s*:\s*["']?(\w+)["']?\s*,?\s*$""")
_S_JSON_REQ = re.compile(r'"required"\s*:\s*\[(.*?)\]')
_S_JSON_PROP = re.compile(r'^\s*"([A-Za-z_][\w-]*)"\s*:\s*\{')
_S_NOT_PROPS = {"required", "type", "description", "example", "enum", "items", "properties", "title", "default", "format", "schema", "content", "responses", "parameters"}


def _schema(path: str, d: dict) -> list[dict]:
    out: list[dict] = []
    stack: list[tuple[int, str]] = []
    added_items: dict[str, set[str]] = {}
    removed_items: list[tuple[str, str]] = []
    for sign, text in d["lines"]:
        k = _S_KEY.match(text)
        if k:
            indent = len(k.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if sign == "-" and stack and stack[-1][1] == "properties":
                out.append(_sig("schema_field_removed", path, f"property {k.group(2)}", "medium"))
            stack.append((indent, k.group(2)))
            continue
        it = _S_ITEM.match(text)
        if it and stack:
            parent = next((key for ind, key in reversed(stack) if ind <= len(it.group(1))), "")
            value = it.group(2).strip("'\"")
            if sign == "+" and parent == "required":
                out.append(_sig("required_added", path, f"required property {value}", "high"))
            elif parent == "enum":
                (added_items.setdefault(parent, set()).add(value) if sign == "+" else removed_items.append((parent, value)) if sign == "-" else None)
    out += [_sig("enum_value_removed", path, f"enum value {v}", "high") for _, v in removed_items if v not in added_items.get("enum", set())]
    removed_paths = {m.group(1) for ln in d["removed"] if (m := _S_PATH.match(ln))}
    added_paths = {m.group(1) for ln in d["added"] if (m := _S_PATH.match(ln))}
    out += [_sig("endpoint_removed", path, f"path {p}", "high") for p in sorted(removed_paths - added_paths)]
    req_removed = {x.strip().strip('"\'') for ln in d["removed"] for m in [_S_JSON_REQ.search(ln)] if m for x in m.group(1).split(",") if x.strip()}
    for ln in d["added"]:
        if m := _S_JSON_REQ.search(ln):
            for x in (x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()):
                if x not in req_removed:
                    out.append(_sig("required_added", path, f"required property {x}", "high"))
    added_props = {m.group(1) for ln in d["added"] if (m := _S_JSON_PROP.match(ln))}
    out += [_sig("schema_field_removed", path, f"property {m.group(1)}", "medium") for ln in d["removed"]
            if (m := _S_JSON_PROP.match(ln)) and m.group(1) not in _S_NOT_PROPS and m.group(1) not in added_props]
    types_removed = [m.group(1) for ln in d["removed"] if (m := _S_TYPE.match(ln))]
    types_added = [m.group(1) for ln in d["added"] if (m := _S_TYPE.match(ln))]
    out += [_sig("type_changed", path, f"type {a} -> {b}", "medium") for a, b in zip(types_removed, types_added) if a != b]
    return out


# -- routes and status codes -----------------------------------------------------------------------------------------
_ROUTE_HINT = re.compile(r"""(?x)(@\w+\.(?P<m1>get|post|put|delete|patch|head|options)\b | \.(?P<m2>GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|get|post|put|delete|patch)\s*\(
    | @(?P<m3>Get|Post|Put|Delete|Patch)Mapping | @\w+\.route\b | \bHandleFunc\s*\( | \bHandle\s*\( | \b(?:re_)?path\s*\()""")
_ROUTE_PATH = re.compile(r"""["'`](/[^"'`\s]*)["'`]""")
_STATUS_HINT = re.compile(r"(?i)status|abort\(|HTTP_\d|WriteHeader|statuscode|ResponseEntity|HTTPException|\)\s*,\s*[1-5]\d\d\b")
_CODE = re.compile(r"\b([1-5]\d\d)\b")


def _route(line: str) -> tuple[str | None, str] | None:
    m = _ROUTE_HINT.search(line)
    p = _ROUTE_PATH.search(line)
    if not (m and p):
        return None
    method = next((m.group(g) for g in ("m1", "m2", "m3") if m.group(g)), None)
    return (method.lower() if method else None, p.group(1))


def _routes(path: str, d: dict) -> list[dict]:
    removed = [r for ln in d["removed"] if (r := _route(ln))]
    added = [r for ln in d["added"] if (r := _route(ln))]
    out = []
    for method, p in removed:
        same_path = [am for am, ap in added if ap == p]
        if not same_path:
            out.append(_sig("route_removed", path, f"{(method or 'ANY').upper()} {p}", "high"))
        elif method and (method not in same_path) and all(m for m in same_path):
            out.append(_sig("route_method_changed", path, f"{method.upper()} {p} -> {', '.join(m.upper() for m in same_path)}", "high"))
    return out


def _status_codes(path: str, d: dict) -> list[dict]:
    norm = lambda ln: _CODE.sub("<N>", ln).strip()
    added = {}
    for ln in d["added"]:
        if _STATUS_HINT.search(ln):
            added.setdefault(norm(ln), []).append(ln)
    out = []
    for ln in d["removed"]:
        if _STATUS_HINT.search(ln) and added.get(norm(ln)):
            new = added[norm(ln)].pop(0)
            if _CODE.findall(ln) != _CODE.findall(new):
                out.append(_sig("status_code_changed", path, f"{', '.join(_CODE.findall(ln))} -> {', '.join(_CODE.findall(new))}", "medium"))
    return out


# -- enums in code -----------------------------------------------------------------------------------------------------
_ENUM_MEMBER = re.compile(r"^\s*([A-Z][A-Za-z0-9_]*)\s*(?:=\s*(.+?))?\s*[,;]?\s*$")


def _enums(path: str, d: dict) -> list[dict]:
    removed = {m.group(1): (m.group(2) or "") for ln in d["removed"] if (m := _ENUM_MEMBER.match(ln)) and len(m.group(1)) > 1}
    added = {m.group(1): (m.group(2) or "") for ln in d["added"] if (m := _ENUM_MEMBER.match(ln)) and len(m.group(1)) > 1}
    out = []
    for name, val in removed.items():
        if name in added:
            if added[name].rstrip(",;") != val.rstrip(",;"):
                out.append(_sig("enum_value_changed", path, f"{name}: {val} -> {added[name]}", "high"))
        else:
            out.append(_sig("enum_value_removed", path, f"{name}{' = ' + val if val else ''}", "medium"))
    return out


# -- public symbols and signatures --------------------------------------------------------------------------------------
_FUNCS = [
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z]\w*)\s*\((.*)\)"),
    re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Z]\w*)\s*\((.*?)\)"),
    re.compile(r"^\s*export\s+(?:async\s+)?function\s+(\w+)\s*\((.*?)\)"),
    re.compile(r"^\s*public\s+(?:static\s+)?[\w<>\[\], ?]+\s+(\w+)\s*\((.*?)\)"),
]


def _params(raw: str) -> dict[str, bool]:
    """``{name: has_default}`` for a parameter list (types and annotations ignored; self/cls/*args/**kwargs skipped)."""
    out, depth, cur = {}, 0, ""
    for ch in raw + ",":
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == "," and depth <= 0:
            p = cur.strip()
            cur = ""
            if p and not p.startswith("*") and p not in ("self", "cls"):
                name = re.split(r"[:=\s]", p.lstrip("&"))[0] if not re.match(r"^[\w<>\[\], ?]+\s+\w+$", p) else p.split()[-1]
                out[name] = "=" in p or p.endswith("?")
        else:
            cur += ch
    return out


def _functions(lines: list[str]) -> dict[str, dict[str, bool]]:
    out = {}
    for ln in lines:
        for rx in _FUNCS:
            if m := rx.match(ln):
                out[m.group(1)] = _params(m.group(2))
                break
    return out


def _symbols(path: str, d: dict) -> list[dict]:
    removed, added = _functions(d["removed"]), _functions(d["added"])
    out = []
    for name, old in removed.items():
        if name.startswith("_"):
            continue
        if name not in added:
            out.append(_sig("public_symbol_removed", path, f"{name}()", "low"))
            continue
        new = added[name]
        gone = [p for p in old if p not in new]
        fresh = [p for p, has_default in new.items() if p not in old and not has_default]
        if fresh:
            out.append(_sig("required_param_added", path, f"{name}(): new required parameter {', '.join(fresh)}", "medium"))
        elif gone:
            out.append(_sig("param_removed", path, f"{name}(): parameter {', '.join(gone)} removed", "medium"))
    return out


# -- database --------------------------------------------------------------------------------------------------------------
_DB_RULES: list[tuple[str, str, re.Pattern]] = [
    ("db_drop", "high", re.compile(r"(?i)\bDROP\s+(?:TABLE|COLUMN|SCHEMA)\b|\b(?:op\.)?drop_(?:table|column)\b|\bremove_column\b|\bdropColumn\b|\bDropColumn\b")),
    ("db_drop", "medium", re.compile(r"(?i)\bDROP\s+(?:INDEX|CONSTRAINT)\b|\bop\.drop_(?:index|constraint)\b|\bremove_index\b")),
    ("db_rename", "high", re.compile(r"(?i)\bRENAME\s+(?:TABLE|COLUMN|TO)\b|\brename_(?:table|column)\b|\brenameColumn\b")),
    ("db_type_changed", "high", re.compile(r"(?i)\bALTER\s+COLUMN\s+\w+\s+(?:SET\s+DATA\s+)?TYPE\b|\bop\.alter_column\(.*\btype_\s*=")),
    ("db_not_null_no_default", "high", re.compile(r"(?i)\bADD\s+COLUMN\b(?!.*\bDEFAULT\b).*\bNOT\s+NULL\b|\badd_column\b(?!.*server_default).*nullable\s*=\s*False")),
    ("db_not_null_added", "medium", re.compile(r"(?i)\bSET\s+NOT\s+NULL\b")),
    ("db_constraint_added", "medium", re.compile(r"(?i)\bADD\s+CONSTRAINT\b.*\b(?:UNIQUE|FOREIGN\s+KEY|CHECK)\b|\bADD\s+(?:UNIQUE|FOREIGN\s+KEY)\b")),
]


def _db(path: str, d: dict) -> list[dict]:
    out = []
    for ln in d["added"]:
        for kind, conf, rx in _DB_RULES:
            if rx.search(ln):
                out.append(_sig(kind, path, ln, conf))
                break
    return out


# -- semantic defaults -----------------------------------------------------------------------------------------------------------
_KV = re.compile(r"""^\s*["']?([A-Za-z_][\w.\-]*)["']?\s*[:=]\s*(.+?)\s*[,;]?\s*$""")
_SEMANTIC_KEY = re.compile(r"(?i)timeout|ttl|limit|max|min|retr(?:y|ies)|threshold|default|interval|size|rate|quota|enable|disable|strict|expire|window|capacity|concurrency|batch|page|mode")


def _defaults(path: str, d: dict, config: bool) -> list[dict]:
    new = {}
    for ln in d["added"]:
        if (m := _KV.match(ln)) and _SEMANTIC_KEY.search(m.group(1)):
            new.setdefault(m.group(1), m.group(2).strip("'\""))
    out = []
    for ln in d["removed"]:
        if (m := _KV.match(ln)) and _SEMANTIC_KEY.search(m.group(1)) and m.group(1) in new:
            old, cur = m.group(2).strip("'\""), new.pop(m.group(1))
            if old != cur:
                out.append(_sig("default_changed", path, f"{m.group(1)}: {old} -> {cur}", "medium" if config else "low"))
    return out


# -- entry points -----------------------------------------------------------------------------------------------------------------------
def analyze_diff(diff: str) -> list[dict]:
    """Signals for every non-test file in a unified diff, each file capped and deduplicated."""
    result: list[dict] = []
    for path, d in parse_diff(diff).items():
        if is_test_file(path) or re.search(r"(^|/)(tests?|__tests__|spec|e2e)(/|$)", path):
            continue
        classes = set(cc.classify_path(path))
        ext = Path(path).suffix.lower()
        sigs: list[dict] = []
        if ext == ".proto":
            sigs += _proto(path, d)
        if ext in (".json", ".yaml", ".yml") and "contract" in classes:
            sigs += _schema(path, d)
        if "migration" in classes or ext == ".sql" or re.search(r"(?i)migrat|alembic", path):
            sigs += _db(path, d)
        if ext in _CODE_EXT:
            sigs += _routes(path, d) + _status_codes(path, d)
            if "enum" in classes:
                sigs += _enums(path, d)
            if classes & {"api", "model", "public_surface", "contract"}:
                sigs += _symbols(path, d)
        if ext != ".proto" and "infra" not in classes and not _MANIFEST.search(path):  # CI, deploy files and dependency manifests are not product behaviour
            sigs += _defaults(path, d, config="config" in classes)
        seen, kept = set(), []
        for s in sigs:
            key = (s["kind"], s["evidence"])
            if key not in seen:
                seen.add(key)
                kept.append(s)
        result += kept[:MAX_SIGNALS_PER_FILE]
    return result


_DECLARED_TITLE = re.compile(r"^\w+(?:\([^)]*\))?!:")
_DECLARED_BODY = re.compile(r"(?i)BREAKING[ -]CHANGES?|backward[- ]incompatible|not backward[- ]compatible|incompatible change|migration (?:is )?required|requires (?:a )?migration")
_DEPRECATION = re.compile(r"(?i)\bdeprecat(?:e|es|ed|ion)\b")
_BREAKING_LABEL = re.compile(r"(?i)breaking|semver[-:]?major|backward[- ]incompatible")


def analyze_text(title: str, body: str, labels: list[str], trust_body: bool = True) -> list[dict]:
    """Breaking changes the author declared (title ``feat!:``, ``BREAKING CHANGE``, label) and deprecations.
    ``trust_body=False`` (dependency bumps, bots) ignores the body: it quotes the upstream project's release notes."""
    title, body = title or "", (body or "") if trust_body else ""
    if _DECLARED_TITLE.search(title):
        return [_sig("declared_breaking", "", title, "high", "title marks a breaking change")]
    if _DECLARED_BODY.search(body):
        m = _DECLARED_BODY.search(body)
        return [_sig("declared_breaking", "", body[max(0, m.start() - 20):m.end() + 100], "high", "description declares a breaking change")]
    if any(_BREAKING_LABEL.search(lb) for lb in labels or []):
        return [_sig("declared_breaking", "", ", ".join(labels), "high", "label marks a breaking change")]
    if _DEPRECATION.search(title) or _DEPRECATION.search(body):
        return [_sig("deprecation", "", (title or body)[:120], "low", "deprecation announced")]
    return []


_TIER1 = {"contract": "contract_changed", "migration": "migration_changed", "config": "config_changed", "api": "api_changed"}


def tier1(classes: dict[str, list[str]]) -> list[dict]:
    """Path-only candidates (low confidence) for a PR whose diff was not read."""
    out = [_sig(kind, "", ", ".join(classes[c][:3]), "low", f"{len(classes[c])} {c} file(s) changed") for c, kind in _TIER1.items() if classes.get(c)]
    em = list(dict.fromkeys((classes.get("enum") or []) + (classes.get("model") or [])))
    if em:
        out.append(_sig("enum_or_model_changed", "", ", ".join(em[:3]), "low", f"{len(em)} enum/model file(s) changed"))
    return out


def priority(classes: dict[str, list[str]], declared: bool, blast: str) -> int:
    """Higher = read this PR's diff first (contract, migration and declared changes, then API, enums/models, config)."""
    base = (4 if declared else 0) + 3 * bool(classes.get("contract")) + 3 * bool(classes.get("migration")) + 2 * bool(classes.get("api")) \
        + 2 * bool(classes.get("enum") or classes.get("model")) + bool(classes.get("config"))
    return base + ({"high": 3, "medium": 1}.get(blast, 0) if base else 0)
