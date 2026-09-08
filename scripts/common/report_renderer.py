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
    """Wrap the implement Markdown report in minimal HTML for browser viewing."""
    status = result.get("status", "UNKNOWN")

    html = md_text
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$", r"<h1>\1</h1>", html, flags=re.MULTILINE)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"`(.+?)`", r"<code>\1</code>", html)
    html = re.sub(r"^- (.+)$", r"<li>\1</li>", html, flags=re.MULTILINE)
    html = html.replace("\n", "<br>\n")

    title = f"Implementation {issue_id} — {status}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 900px; margin: 40px auto; padding: 0 20px; color: #24292e; }}
  h1 {{ border-bottom: 2px solid #e1e4e8; padding-bottom: 8px; }}
  h2 {{ border-bottom: 1px solid #e1e4e8; padding-bottom: 4px; margin-top: 24px; }}
  code {{ background: #f6f8fa; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
  li {{ margin-left: 20px; }}
  strong {{ font-weight: 600; }}
</style>
</head>
<body>
{html}
</body>
</html>"""


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
                output.append('<table><thead><tr>')
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

    body = "\n".join(output)
    escaped_title = _escape_html(title)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escaped_title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 1000px; margin: 40px auto; padding: 0 20px; color: #24292e; line-height: 1.6; }}
  h1 {{ border-bottom: 2px solid #e1e4e8; padding-bottom: 8px; }}
  h2 {{ border-bottom: 1px solid #e1e4e8; padding-bottom: 4px; margin-top: 24px; }}
  h3 {{ margin-top: 18px; }}
  code {{ background: #f6f8fa; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; font-family: monospace; }}
  pre {{ background: #f6f8fa; padding: 12px; border-radius: 6px; overflow-x: auto; }}
  pre code {{ background: none; padding: 0; }}
  ul, ol {{ margin-left: 24px; margin-bottom: 8px; }}
  li {{ margin-bottom: 4px; }}
  strong {{ font-weight: 600; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  td, th {{ border: 1px solid #e1e4e8; padding: 6px 12px; text-align: left; }}
  th {{ background: #f6f8fa; font-weight: 600; }}
  hr {{ border: none; border-top: 1px solid #e1e4e8; margin: 16px 0; }}
  p {{ margin: 8px 0; }}
</style>
</head>
<body>
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
