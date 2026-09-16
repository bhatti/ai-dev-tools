"""Render standup signals + brief into a formatted HTML report.

Usage:
    python -m scripts.standup.render_html

Reads:  /workspace/signals.json
        /workspace/risk_report.md     (optional, Markdown)
Writes: /workspace/reports/report.html

Exit codes: 0=done, 1=error
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from scripts.common.config import load_config, get_workspace_dir


# ---------------------------------------------------------------------------
# Markdown → simple HTML (enough for risk_report)
# ---------------------------------------------------------------------------

def _md_to_html(text: str) -> str:
    """Convert the subset of Markdown/mrkdwn used in standup reports to HTML."""
    lines = text.splitlines()
    out: list[str] = []
    in_table = False
    list_tag = ""  # "ul" or "ol" — tracks which list is open
    in_code = False

    def _close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = ""

    for line in lines:
        # Code fences
        if line.strip().startswith("```"):
            if in_code:
                out.append("</pre></code>")
                in_code = False
            else:
                _close_list()
                out.append("<code><pre>")
                in_code = True
            continue
        if in_code:
            out.append(_esc(line))
            continue

        # Table rows
        if line.strip().startswith("|"):
            _close_list()
            if not in_table:
                out.append('<table class="table table-sm table-bordered">')
                in_table = True
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r"^[-: ]+$", c) for c in cols):
                continue  # separator row
            tag = "th" if not any("<td>" in r for r in out[-3:]) else "td"
            row_html = "".join(f"<{tag}>{_inline_md(_esc(c))}</{tag}>" for c in cols)
            out.append(f"<tr>{row_html}</tr>")
            continue
        else:
            if in_table:
                out.append("</table>")
                in_table = False

        # Headings
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            _close_list()
            level = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{level}>{_inline_md(_esc(m.group(2)))}</h{level}>")
            continue

        # HR
        if re.match(r"^---+$", line.strip()):
            _close_list()
            out.append("<hr>")
            continue

        # Unordered bullet
        if re.match(r"^[-*]\s+", line):
            if list_tag != "ul":
                _close_list()
                out.append("<ul>")
                list_tag = "ul"
            out.append(f"<li>{_inline_md(_esc(line[2:].strip()))}</li>")
            continue

        # Ordered list
        if re.match(r"^\d+\.\s+", line):
            if list_tag != "ol":
                _close_list()
                out.append("<ol>")
                list_tag = "ol"
            out.append(f"<li>{_inline_md(_esc(re.sub(r'^\d+\.\s+', '', line)))}</li>")
            continue

        # Close list on blank or non-list line
        if line.strip() == "":
            _close_list()
            out.append("")
        else:
            _close_list()
            out.append(f"<p>{_inline_md(_esc(line))}</p>")

    if in_table:
        out.append("</table>")
    _close_list()
    if in_code:
        out.append("</pre></code>")

    return "\n".join(out)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_EMOJI_MAP = {
    ":white_check_mark:": "✅", ":warning:": "⚠️",
    ":red_circle:": "🔴", ":large_yellow_circle:": "🟡",
    ":rotating_light:": "🚨", ":bust_in_silhouette:": "👤",
    ":question:": "❓", ":paperclip:": "📎",
    ":mag:": "🔍", ":chart_with_upwards_trend:": "📈",
    ":speech_balloon:": "💬", ":clock1:": "🕐",
    ":fire:": "🔥", ":bell:": "🔔",
}


def _inline_md(s: str) -> str:
    """Apply inline Markdown/mrkdwn: bold, italic, code, links, emoji shortcuts.

    Input has already been HTML-escaped by _esc(), so < > & are entities.
    Slack mrkdwn links <url|text> arrive as &lt;url|text&gt; — convert those first.
    """
    for alias, emoji in _EMOJI_MAP.items():
        s = s.replace(alias, emoji)
    # Slack mrkdwn links: &lt;https://...url|display text&gt; → <a href="url">text</a>
    s = re.sub(r"&lt;(https?://[^|&]+)\|([^&]+)&gt;", r'<a href="\1">\2</a>', s)
    # Bold **text** or *text* (mrkdwn)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*(.+?)\*", r"<strong>\1</strong>", s)
    # Italic _text_
    s = re.sub(r"_(.+?)_", r"<em>\1</em>", s)
    # Inline code
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    # Markdown links [text](url)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    return s


# ---------------------------------------------------------------------------
# Board status table from signals
# ---------------------------------------------------------------------------

DONE_STATUSES: frozenset[str] = frozenset({"done", "closed", "resolved", "won't fix", "wont fix", "rejected"})


def _board_status_rows(signals: dict) -> str:
    sprints = signals.get("all_sprints", [])
    issues = signals.get("issues", [])
    today = date.today()

    total = len(issues)
    done_n = sum(1 for i in issues if i.get("status", "").lower() in DONE_STATUSES)
    in_progress = sum(1 for i in issues if i.get("status", "").lower() in ("in progress", "in review", "review"))
    not_started = total - done_n - in_progress

    rows = ""
    seen_sprint_ids: set = set()
    for s in sprints:
        sid = s.get("id")
        board = _esc(s.get("board", "—"))
        sprint_name = _esc(s.get("name", "—"))
        end_date_str = s.get("end_date", "")
        days_left = "?"
        if end_date_str:
            try:
                end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                days_left = str((end_dt.date() - today).days)
            except ValueError:
                pass

        if sid in seen_sprint_ids:
            # Same sprint, different board — show board alias
            rows += f"<tr><td>{board}</td><td><em>shared: {sprint_name}</em></td><td colspan='5' class='text-muted'>same issue pool as above</td></tr>"
        else:
            seen_sprint_ids.add(sid)
            rows += (
                f"<tr><td>{board}</td><td>{sprint_name}</td>"
                f"<td class='text-center'>{total}</td>"
                f"<td class='text-center text-success'>{done_n}</td>"
                f"<td class='text-center text-warning'>{in_progress}</td>"
                f"<td class='text-center'>{not_started}</td>"
                f"<td class='text-center'>{days_left}</td></tr>"
            )
    return rows


# ---------------------------------------------------------------------------
# Full HTML template
# ---------------------------------------------------------------------------

_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Standup Report — {report_date}</title>
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
    crossorigin="anonymous">
  <style>
    body {{ font-size: .9rem; }}
    h3 {{ margin-top: 1.5rem; }}
    pre {{ background:#f8f9fa; padding:.75rem; border-radius:.375rem; font-size:.8rem; }}
    .risk-high {{ border-left: 4px solid #dc3545; padding-left:.75rem; margin-bottom:.75rem; }}
    .risk-med  {{ border-left: 4px solid #ffc107; padding-left:.75rem; margin-bottom:.75rem; }}
    .risk-low  {{ border-left: 4px solid #0dcaf0; padding-left:.75rem; margin-bottom:.75rem; }}
    .brief-section {{ background:#f8f9fa; border-radius:.5rem; padding:1rem 1.25rem; margin-bottom:1rem; }}
    .brief-section hr {{ border-color: #dee2e6; }}
  </style>
</head>
<body class="container-fluid py-3">

<div class="d-flex align-items-center justify-content-between mb-3">
  <h2 class="mb-0">📋 Standup Report — {report_date}</h2>
  <span class="text-muted small">Generated {generated_at}</span>
</div>

<!-- ── Board Status ──────────────────────────────────────────────────────── -->
<h3>Board Status</h3>
<table class="table table-bordered table-sm">
  <thead class="table-dark">
    <tr>
      <th>Board</th><th>Sprint</th>
      <th class="text-center">Total</th>
      <th class="text-center">Done</th>
      <th class="text-center">Active</th>
      <th class="text-center">Not Started</th>
      <th class="text-center">Days Left</th>
    </tr>
  </thead>
  <tbody>
    {board_rows}
  </tbody>
</table>

{brief_html}

{risk_section}

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)

    signals_path = workspace_dir / "signals.json"
    if not signals_path.exists():
        print("ERROR: signals.json not found — run gather step first", file=sys.stderr)
        sys.exit(1)

    signals = json.loads(signals_path.read_text())

    risk_report_path = workspace_dir / "risk_report.md"
    risk_md = risk_report_path.read_text().strip() if risk_report_path.exists() else ""
    risk_section = (
        f'<!-- ── Risk Report ──────────────────────────────────────────────────────── -->\n'
        f'<h3>Risk Report</h3>\n<div class="risk-detail">\n{_md_to_html(risk_md)}\n</div>'
        if risk_md else ""
    )

    brief_path = workspace_dir / "standup_brief.md"
    brief_html = ""
    if brief_path.exists():
        brief_md = brief_path.read_text().strip()
        if brief_md:
            brief_html = f'<div class="brief-section">\n{_md_to_html(brief_md)}\n</div>'
            print(f"[render_html] included standup_brief.md ({len(brief_md)} chars)", flush=True)

    report_date = date.today().isoformat()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = _HTML.format(
        report_date=report_date,
        generated_at=generated_at,
        board_rows=_board_status_rows(signals),
        brief_html=brief_html,
        risk_section=risk_section,
    )

    reports_dir = workspace_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = reports_dir / "report.html"
    out_path.write_text(html)
    print(f"[render_html] written: {out_path}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    from scripts.common.entrypoint import run_main
    run_main(main, "render_html_result.json")
