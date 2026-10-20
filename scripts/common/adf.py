"""Atlassian Document Format (ADF), the JSON a Jira description is stored in.

``to_text`` flattens a document for reading and keeps its structure (headings, lists, tables) and every piece of text it
carries, which ``jira_api.extract_adf_text`` does not. ``from_markdown`` turns generated text into ADF nodes (with inline
marks, nested lists, quotes and task lists). A generated section is ONE ``panel`` node holding its heading and content, so its
boundary is a node, never inferred from heading levels or rules: replacing it cannot touch anything around it. Writing plain
text over a description would destroy tables, links and formatting, so callers only ever add or swap that one node.
"""

from __future__ import annotations

import copy
import os
import re
from datetime import datetime, timezone

_MAX_DEPTH = 200  # far beyond any real document, and below Python's recursion limit; past it the text is marked, never silently dropped
_TASK = re.compile(r"^[-*+]\s+\[([ xX])\]\s+(.*)$")
_BULLET = re.compile(r"^([-*+])\s+(.*)$")
_ORDERED = re.compile(r"^(\d+)[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_RULE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_INLINE = re.compile(r"`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*|(?<![\w*])\*([^*\s][^*]*)\*(?![\w*])")


class NotReplaceable(ValueError):
    """The heading exists but not as a panel this tool wrote, so replacing it could damage text a person wrote."""


# ── reading ───────────────────────────────────────────────────────────────────

def _date(attrs: dict) -> str:
    try:
        return datetime.fromtimestamp(int(attrs["timestamp"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return ""


def _indented(text: str) -> str:
    """Continuation lines of a list item (a nested list or a second paragraph) are indented under it."""
    first, *rest = text.strip().splitlines() or [""]
    return "\n".join([first, *("  " + line if line.strip() else line for line in rest)])


def _params(value: object) -> list[str]:
    """Every string inside extension parameters (macro titles, values): they carry words a reader needs."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        return [t for v in value.values() for t in _params(v)]
    if isinstance(value, list):
        return [t for v in value for t in _params(v)]
    return []


def _extension(attrs: dict) -> str:
    words = [attrs.get("text") or attrs.get("title") or attrs.get("extensionKey", ""), *_params(attrs.get("parameters"))]
    return " ".join(w for w in words if w)


def _card(attrs: dict) -> str:
    data = attrs.get("data") if isinstance(attrs.get("data"), dict) else {}
    return attrs.get("url") or data.get("url") or data.get("name") or ""


def to_text(node: object, depth: int = 0) -> str:
    """Readable text of an ADF node: ``#`` headings, ``- `` / ``1. `` / ``- [ ]`` items, ``>`` quotes, ``|`` between cells, link
    URLs in parentheses. Nothing that carries words is dropped: unknown node types still yield their children, and a plain string
    is returned as it is. Anything else that is not a node gives ''."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if depth > _MAX_DEPTH:
        return "[content nested too deep to read]"
    kind, attrs, kids = node.get("type"), node.get("attrs") or {}, node.get("content") or []
    if kind == "text":
        text = node.get("text", "")
        href = next(((m.get("attrs") or {}).get("href") for m in node.get("marks") or [] if m.get("type") == "link"), None)
        return f"{text} ({href})" if href and href != text else text
    simple = {"hardBreak": "\n", "rule": "\n---\n", "mention": attrs.get("text") or f"[mention: {attrs.get('id', '')}]",
              "status": attrs.get("text", ""), "emoji": attrs.get("text") or attrs.get("shortName", ""), "date": _date(attrs),
              "placeholder": attrs.get("text", ""), "inlineCard": _card(attrs), "blockCard": _card(attrs), "embedCard": _card(attrs),
              "extension": _extension(attrs), "inlineExtension": _extension(attrs)}
    if kind in simple:
        return simple[kind]
    if kind in ("media", "mediaInline"):
        return f"[attachment: {attrs.get('alt') or attrs.get('id') or 'file'}]"
    parts = [to_text(k, depth + 1) for k in kids]  # each child is rendered once, whatever the node type
    inner = "".join(parts)
    if kind == "heading":
        return f"\n{'#' * int(attrs.get('level') or 2)} {inner.strip()}\n"
    if kind == "bulletList":
        return "".join(f"- {_indented(p)}\n" for p in parts)
    if kind == "orderedList":
        start = int(attrs.get("order") or 1)
        return "".join(f"{i}. {_indented(p)}\n" for i, p in enumerate(parts, start))
    if kind == "taskList":
        return "".join(f"- [{'x' if (k.get('attrs') or {}).get('state') == 'DONE' else ' '}] {_indented(p)}\n" for k, p in zip(kids, parts))
    if kind == "decisionList":
        return "".join(f"- [{'decided' if (k.get('attrs') or {}).get('state') == 'DECIDED' else 'open'}] {_indented(p)}\n" for k, p in zip(kids, parts))
    if kind == "codeBlock":
        return f"\n```\n{inner}\n```\n"
    if kind == "blockquote":
        return "".join(f"> {line}\n" for line in inner.strip().splitlines()) or ""
    if kind in ("expand", "nestedExpand") and attrs.get("title"):
        return f"\n{attrs['title']}\n{inner}"
    if kind in ("tableCell", "tableHeader"):
        return " ".join(part.strip() for part in inner.splitlines() if part.strip())
    if kind == "tableRow":
        return " | ".join(parts) + "\n"
    if kind in ("paragraph", "taskItem", "decisionItem", "caption"):
        return inner + "\n"
    return inner


# ── writing ───────────────────────────────────────────────────────────────────

def _marks(text: str) -> list[dict]:
    out: list[dict] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append({"type": "text", "text": text[pos:m.start()]})
        if m.group(1) is not None:
            out.append({"type": "text", "text": m.group(1), "marks": [{"type": "code"}]})
        elif m.group(2) is not None:
            out.append({"type": "text", "text": m.group(2), "marks": [{"type": "link", "attrs": {"href": m.group(3)}}]})
        elif m.group(4) is not None:
            out.append({"type": "text", "text": m.group(4), "marks": [{"type": "strong"}]})
        else:
            out.append({"type": "text", "text": m.group(5), "marks": [{"type": "em"}]})
        pos = m.end()
    if pos < len(text):
        out.append({"type": "text", "text": text[pos:]})
    return out or [{"type": "text", "text": text}]


def _paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": _marks(text)}


def _list_line(line: str) -> tuple[int, str, str, str] | None:
    """(indent, list kind, text, marker detail) of a list line, else None."""
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    for pattern, kind in ((_TASK, "taskList"), (_BULLET, "bulletList"), (_ORDERED, "orderedList")):
        m = pattern.match(stripped)
        if m:
            if kind == "taskList":
                return indent, kind, m.group(2).strip(), "DONE" if m.group(1) in "xX" else "TODO"
            return indent, kind, m.group(2).strip(), m.group(1) if kind == "orderedList" else ""
    return None


def _local_id() -> str:
    return os.urandom(8).hex()


def _item(kind: str, text: str, detail: str) -> dict:
    if kind == "taskList":
        return {"type": "taskItem", "attrs": {"localId": _local_id(), "state": detail}, "content": _marks(text)}
    return {"type": "listItem", "content": [_paragraph(text)]}


def _nests(outer_kind: str, line_kind: str) -> bool:
    """A deeper line nests under the previous item only where ADF allows it: bullet / numbered lists inside bullet / numbered items.
    Task items cannot hold other lists, and a task list cannot hold bullets, so those start a new list instead."""
    return outer_kind in ("bulletList", "orderedList") and line_kind in ("bulletList", "orderedList")


def _list(lines: list[tuple[int, str, str, str]]) -> dict:
    """One list node (with nested lists) from consecutive list lines; a deeper indent nests under the previous item."""
    base = lines[0][0]
    kind = lines[0][1]
    node: dict = {"type": kind, "content": []}
    if kind == "taskList":
        node["attrs"] = {"localId": _local_id()}
    if kind == "orderedList" and lines[0][3].isdigit() and int(lines[0][3]) != 1:
        node["attrs"] = {"order": int(lines[0][3])}
    i = 0
    while i < len(lines):
        indent, k, text, detail = lines[i]
        if indent > base and node["content"]:
            j = i
            while j < len(lines) and lines[j][0] > base:
                j += 1
            node["content"][-1]["content"].append(_list(lines[i:j]))
            i = j
            continue
        node["content"].append(_item(kind, text, detail))
        i += 1
    return node


def from_markdown(text: str) -> list[dict]:
    """Nodes for headings, paragraphs, bullet / numbered / task lists (nested by indentation), ``>`` quotes, fenced code and ``---`` rules,
    with ``**bold**``, ``*italic*``, ```code``` and ``[text](url)`` as marks. A markdown table becomes a code block (every cell is kept,
    ADF tables cannot live inside a panel). Nothing in the input is dropped."""
    nodes: list[dict] = []
    pending: list[tuple[int, str, str, str]] = []
    table: list[str] = []
    quote: list[str] = []
    code: list[str] | None = None

    def flush() -> None:
        nonlocal pending, table, quote
        if pending:
            nodes.append(_list(pending))
        if table:
            nodes.append({"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(table)}]})
        if quote:
            nodes.append({"type": "blockquote", "content": [_paragraph(" ".join(quote))]})
        pending, table, quote = [], [], []

    for raw in text.splitlines():
        line = raw.rstrip()
        if code is not None:
            if line.strip().startswith("```"):
                nodes.append({"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(code)}]})
                code = None
            else:
                code.append(raw)
            continue
        if line.strip().startswith("```"):
            flush()
            code = []
            continue
        item = _list_line(line)
        if item:
            if table or quote:
                flush()
            if pending and ((item[0] <= pending[0][0] and item[1] != pending[0][1])
                            or (item[0] > pending[0][0] and not _nests(pending[-1][1], item[1]))
                            or (item[0] == pending[0][0] and item[1] == "taskList" and pending[0][1] != "taskList")):
                flush()
            pending.append(item)
            continue
        if line.lstrip().startswith("|"):
            if pending or quote:
                flush()
            table.append(line.strip())
            continue
        if line.lstrip().startswith(">"):
            if pending or table:
                flush()
            quote.append(line.lstrip()[1:].strip())
            continue
        flush()
        heading = _HEADING.match(line)
        if heading:
            nodes.append({"type": "heading", "attrs": {"level": len(heading.group(1))}, "content": [{"type": "text", "text": heading.group(2)}]})
        elif _RULE.match(line.strip()):
            nodes.append({"type": "rule"})
        elif line.strip():
            nodes.append(_paragraph(line.strip()))
    if code is not None:  # an unterminated fence still keeps what was written
        nodes.append({"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(code)}]})
    flush()
    return nodes


# ── sections ──────────────────────────────────────────────────────────────────

def _heading_text(node: dict) -> str:
    return to_text(node).strip().lstrip("#").strip()


FOOTER = "Generated by you-got-skills (ygs). Re-running with --force replaces this box."


def _is_generated(panel: dict) -> bool:
    """True for a panel this tool wrote: it ends with the provenance footer, so a person's panel with the same heading is never mistaken for it."""
    last = (panel.get("content") or [{}])[-1]
    return last.get("type") == "paragraph" and to_text(last).strip() == FOOTER


def _mentions(node: object, want: str) -> bool:
    """True when ``want`` is the text of a heading anywhere inside ``node`` (a section a person moved into an expand or layout is still there)."""
    if not isinstance(node, dict):
        return False
    if node.get("type") == "heading" and _heading_text(node).lower() == want:
        return True
    return any(_mentions(k, want) for k in node.get("content") or [])


def _find(content: list[dict], heading: str) -> tuple[int, bool] | None:
    """(index, replaceable): our generated panel at the top level, else any other place the heading appears (not replaceable)."""
    want = heading.strip().lower()
    for i, node in enumerate(content):
        if node.get("type") == "panel" and _is_generated(node):
            first = (node.get("content") or [{}])[0]
            if first.get("type") == "heading" and _heading_text(first).lower() == want:
                return i, True
    for i, node in enumerate(content):
        if _mentions(node, want):
            return i, False
    return None


def _document(doc: dict | None) -> dict:
    return copy.deepcopy(doc) if doc else {"type": "doc", "version": 1, "content": []}


def _panel(nodes: list[dict]) -> dict:
    footer = {"type": "paragraph", "content": [{"type": "text", "text": FOOTER, "marks": [{"type": "em"}]}]}
    return {"type": "panel", "attrs": {"panelType": "note"}, "content": [*copy.deepcopy(nodes), footer]}


def has_section(doc: dict | None, heading: str) -> bool:
    return _find((doc or {}).get("content", []), heading) is not None


def append_section(doc: dict | None, heading: str, nodes: list[dict]) -> dict:
    """A copy of ``doc`` with one panel (``nodes``, whose first node is the heading) added at the end; the existing nodes are unchanged."""
    out = _document(doc)
    out["content"] = [*out.get("content", []), _panel(nodes)]
    return out


def replace_section(doc: dict | None, heading: str, nodes: list[dict]) -> dict:
    """A copy of ``doc`` with the panel called ``heading`` swapped for a new one. KeyError when absent; :class:`NotReplaceable`
    when the heading is plain document text (written by a person), which is never rewritten."""
    out = _document(doc)
    found = _find(out.get("content", []), heading)
    if not found:
        raise KeyError(heading)
    index, replaceable = found
    if not replaceable:
        raise NotReplaceable(f"'{heading}' is part of the description's own text, not a section written by this tool")
    out["content"][index] = _panel(nodes)
    return out


def replaceable(doc: dict | None, heading: str) -> bool:
    """True when the section exists as a panel this tool wrote (so ``replace_section`` will work)."""
    found = _find((doc or {}).get("content", []), heading)
    return bool(found and found[1])


def split_text(doc: dict | None, heading: str) -> tuple[str, str | None]:
    """(text of everything outside the section, text of the section or None): used to verify a write left the rest alone."""
    content = (doc or {}).get("content", [])
    found = _find(content, heading)
    if not found or not found[1]:
        return to_text(doc), None
    index = found[0]
    rest = {"type": "doc", "content": content[:index] + content[index + 1:]}
    return to_text(rest), to_text(content[index])
