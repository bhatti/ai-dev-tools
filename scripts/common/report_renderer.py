"""Shared HTML/Markdown report renderer for implement self-review artifacts."""
from __future__ import annotations

import re

_STATUS_EMOJI = {
    "DONE": "✅",
    "TESTS_FAILING": "🔴",
    "CANNOT_IMPLEMENT": "🚫",
    "PARTIAL": "🟡",
    "ERROR": "❌",
}


def render_implement_report_md(issue_id: str, result: dict) -> str:
    """Render implement self-review result as a Markdown report."""
    status = result.get("status", "UNKNOWN")
    summary = result.get("summary", "")
    files_changed = result.get("files_changed", [])
    commits = result.get("commits", 0)
    tests_status = result.get("tests_status", "")
    reason = result.get("reason", "")

    emoji = _STATUS_EMOJI.get(status, "❓")
    lines = [
        f"# {emoji} Implementation — {status}",
        "",
        f"**Issue:** {issue_id}",
        "",
    ]

    if summary:
        lines += ["## Summary", "", summary, ""]

    if reason:
        lines += ["## Details", "", reason, ""]

    if files_changed:
        lines += [f"## Changed Files ({len(files_changed)})", ""]
        for f in files_changed:
            lines.append(f"- `{f}`")
        lines.append("")

    meta = []
    if commits:
        meta.append(f"Commits: {commits}")
    if tests_status:
        meta.append(f"Tests: {tests_status}")
    if meta:
        lines += ["## Run Info", "", "  ".join(meta), ""]

    return "\n".join(l for l in lines if l is not None)


def render_implement_report_html(issue_id: str, result: dict, md_text: str) -> str:
    """Wrap the implement Markdown report in HTML for browser viewing."""
    status = result.get("status", "UNKNOWN")
    title = f"Implementation {issue_id} — {status}"
    return render_simple_html(title, md_text)


_EMOJI_LABELS: dict[str, str] = {
    "🔴": "High",
    "🟡": "Medium",
    "🟢": "Low / Healthy",
    "🐛": "Bug fix",
    "✨": "Feature",
    "♻️": "Refactor",
    "🔧": "Chore",
    "🔒": "Security",
    "🧪": "Test",
    "📝": "Documentation",
    "❓": "Unknown type",
    "🔥": "Hotspot",
    "⚠️": "Warning",
    "🔵": "Current position",
    "░": "Gauge empty",
}

_EMOJI_RE = re.compile("|".join(re.escape(e) for e in _EMOJI_LABELS))


def _annotate_emoji(html: str) -> str:
    """Wrap known emoji in <span title="..."> for hover labels in HTML reports."""
    def _repl(m: re.Match) -> str:
        emoji = m.group(0)
        label = _EMOJI_LABELS.get(emoji, "")
        return f'<span title="{label}">{emoji}</span>' if label else emoji
    return _EMOJI_RE.sub(_repl, html)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_md_to_html(text: str) -> str:
    """Convert inline markdown (bold, italic, code, links) to HTML."""
    # Inline code first (prevents double-processing)
    parts: list[str] = []
    remainder = text
    while True:
        start = remainder.find("`")
        if start == -1:
            parts.append(_escape_html(remainder))
            break
        parts.append(_escape_html(remainder[:start]))
        end = remainder.find("`", start + 1)
        if end == -1:
            parts.append(_escape_html(remainder[start:]))
            break
        code = remainder[start + 1:end]
        parts.append(f"<code>{_escape_html(code)}</code>")
        remainder = remainder[end + 1:]
    text = "".join(parts)
    # Bold+italic ***
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<strong><em>\1</em></strong>', text)
    # Bold **
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Italic * (not bold) — only word-boundary to avoid matching list bullets
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)
    # Links [text](url)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    return text


def render_simple_html(title: str, md_text: str) -> str:
    """Convert Markdown to HTML with proper code blocks, tables, and lists."""
    lines = md_text.splitlines()
    output: list[str] = []
    in_code_block = False
    code_lang = ""
    in_table = False
    table_header_done = False
    list_tag = ""  # "ul" or "ol"

    def _flush_list() -> None:
        nonlocal list_tag
        if list_tag:
            output.append(f"</{list_tag}>")
            list_tag = ""

    def _flush_table() -> None:
        nonlocal in_table, table_header_done
        if in_table:
            output.append("</tbody></table>")
            in_table = False
            table_header_done = False

    for line in lines:
        # Fenced code block
        if line.startswith("```") or line.startswith("~~~"):
            fence_char = line[:3]
            if not in_code_block:
                _flush_list()
                _flush_table()
                in_code_block = True
                code_lang = line[3:].strip()
                lang_attr = f' class="language-{_escape_html(code_lang)}"' if code_lang else ""
                output.append(f"<pre><code{lang_attr}>")
            else:
                in_code_block = False
                output.append("</code></pre>")
            continue

        if in_code_block:
            output.append(_escape_html(line))
            continue

        # Horizontal rule
        if re.match(r"^[-*_]{3,}\s*$", line):
            _flush_list()
            _flush_table()
            output.append("<hr>")
            continue

        # Headings
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            _flush_list()
            _flush_table()
            level = len(m.group(1))
            output.append(f"<h{level}>{_inline_md_to_html(m.group(2))}</h{level}>")
            continue

        # Table rows
        if line.startswith("|") and "|" in line[1:]:
            _flush_list()
            cells = [c.strip() for c in line.strip("|").split("|")]
            # Separator row (|---|---|)
            if all(re.match(r"^[:\-\s]+$", c) for c in cells if c):
                if in_table and not table_header_done:
                    output.append("</tr></thead><tbody>")
                    table_header_done = True
                continue
            if not in_table:
                output.append('<table class="table table-bordered table-sm table-hover table-striped"><thead><tr>')
                in_table = True
                table_header_done = False
                tag = "th"
            else:
                tag = "td"
            cell_html = "".join(f"<{tag}>{_inline_md_to_html(c)}</{tag}>" for c in cells)
            if not table_header_done:
                output.append(f"{cell_html}")
            else:
                output.append(f"<tr>{cell_html}</tr>")
            continue

        # Non-table line — close any open table
        if in_table:
            _flush_table()

        # Unordered list
        m = re.match(r"^[ \t]*[-*+] (.+)$", line)
        if m:
            if list_tag != "ul":
                _flush_list()
                output.append("<ul>")
                list_tag = "ul"
            output.append(f"<li>{_inline_md_to_html(m.group(1))}</li>")
            continue

        # Ordered list
        m = re.match(r"^[ \t]*\d+\. (.+)$", line)
        if m:
            if list_tag != "ol":
                _flush_list()
                output.append("<ol>")
                list_tag = "ol"
            output.append(f"<li>{_inline_md_to_html(m.group(1))}</li>")
            continue

        # Close list on blank or non-list line
        _flush_list()

        # Blank line → paragraph break
        if not line.strip():
            output.append("")
            continue

        output.append(f"<p>{_inline_md_to_html(line)}</p>")

    _flush_list()
    _flush_table()
    if in_code_block:
        output.append("</code></pre>")

    body = _annotate_emoji("\n".join(output))
    escaped_title = _escape_html(title)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
    crossorigin="anonymous">
  <style>
    body {{ font-size: .9rem; }}
    h1 {{ border-bottom: 2px solid #dee2e6; padding-bottom: .5rem; margin-bottom: 1rem; }}
    h2 {{ border-bottom: 1px solid #dee2e6; padding-bottom: .3rem; margin-top: 1.5rem; margin-bottom: .75rem; }}
    h3 {{ margin-top: 1.25rem; margin-bottom: .5rem; }}
    h4, h5, h6 {{ margin-top: 1rem; margin-bottom: .4rem; }}
    pre {{ background: #f8f9fa; padding: .75rem; border-radius: .375rem; font-size: .8rem; overflow-x: auto; }}
    pre code {{ background: none; padding: 0; }}
    .risk-high {{ border-left: 4px solid #dc3545; padding-left: .75rem; margin-bottom: .75rem; }}
    .risk-med  {{ border-left: 4px solid #ffc107; padding-left: .75rem; margin-bottom: .75rem; }}
    .risk-low  {{ border-left: 4px solid #0dcaf0; padding-left: .75rem; margin-bottom: .75rem; }}
  </style>
</head>
<body class="container py-4">
{body}
</body>
</html>"""


def build_implement_slack_text(issue_id: str, result: dict) -> str:
    """Build compact Slack mrkdwn message for implement result."""
    status = result.get("status", "UNKNOWN")
    summary = result.get("summary", "")
    files_changed = result.get("files_changed", [])
    commits = result.get("commits", 0)
    tests_status = result.get("tests_status", "")
    reason = result.get("reason", "")

    emoji = _STATUS_EMOJI.get(status, "❓")
    lines = [f"{emoji} *Implementation {issue_id} — {status}*"]

    if summary:
        lines += ["", summary]

    if reason:
        lines += ["", f"_{reason}_"]

    meta = []
    if commits:
        meta.append(f"commits: {commits}")
    if tests_status:
        meta.append(f"tests: {tests_status}")
    if files_changed:
        meta.append(f"files: {len(files_changed)}")
    if meta:
        lines += ["", " | ".join(meta)]

    return "\n".join(lines)
