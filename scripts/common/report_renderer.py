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


def render_simple_html(title: str, md_text: str) -> str:
    """Wrap any Markdown text in minimal HTML for browser viewing."""
    import re
    html = md_text
    html = re.sub(r"^#### (.+)$", r"<h4>\1</h4>", html, flags=re.MULTILINE)
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$", r"<h2>\1</h2>", html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$", r"<h1>\1</h1>", html, flags=re.MULTILINE)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"_(.+?)_", r"<em>\1</em>", html)
    html = re.sub(r"`(.+?)`", r"<code>\1</code>", html)
    html = re.sub(r"^\| (.+)$", r"<tr><td>\1</td></tr>", html, flags=re.MULTILINE)
    html = re.sub(r"^[-*] (.+)$", r"<li>\1</li>", html, flags=re.MULTILINE)
    html = html.replace("\n\n", "</p>\n<p>")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 1000px; margin: 40px auto; padding: 0 20px; color: #24292e; }}
  h1 {{ border-bottom: 2px solid #e1e4e8; padding-bottom: 8px; }}
  h2 {{ border-bottom: 1px solid #e1e4e8; padding-bottom: 4px; margin-top: 24px; }}
  h3 {{ margin-top: 18px; }}
  code {{ background: #f6f8fa; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }}
  li {{ margin-left: 20px; margin-bottom: 4px; }}
  strong {{ font-weight: 600; }}
  pre {{ background: #f6f8fa; padding: 12px; border-radius: 6px; overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  td, th {{ border: 1px solid #e1e4e8; padding: 6px 12px; text-align: left; }}
  th {{ background: #f6f8fa; }}
</style>
</head>
<body>
<p>{html}</p>
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
