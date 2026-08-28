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
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from scripts.common.config import load_config, get_workspace_dir


# ---------------------------------------------------------------------------
# Markdown → simple HTML (enough for risk_report)
# ---------------------------------------------------------------------------

def _md_to_html(text: str) -> str:
    """Convert the subset of Markdown used in risk_report to HTML."""
    lines = text.splitlines()
    out: list[str] = []
    in_table = False
    in_ul = False
    in_code = False

    for line in lines:
        # Code fences
        if line.strip().startswith("```"):
            if in_code:
                out.append("</pre></code>")
                in_code = False
            else:
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                out.append("<code><pre>")
                in_code = True
            continue
        if in_code:
            out.append(_esc(line))
            continue

        # Table rows
        if line.strip().startswith("|"):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_table:
                out.append('<table class="table table-sm table-bordered">')
                in_table = True
            cols = [c.strip() for c in line.strip().strip("|").split("|")]
            # separator row
            if all(re.match(r"^[-: ]+$", c) for c in cols):
                continue
            tag = "th" if not any("<td>" in r for r in out[-3:]) else "td"
            # detect header by checking if previous non-empty lines were all th
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
            if in_ul:
                out.append("</ul>")
                in_ul = False
            level = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{level}>{_inline_md(_esc(m.group(2)))}</h{level}>")
            continue

        # HR
        if re.match(r"^---+$", line.strip()):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append("<hr>")
            continue

        # Bullets
        if re.match(r"^[-*]\s+", line):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline_md(_esc(line[2:].strip()))}</li>")
            continue
        if re.match(r"^\d+\.\s+", line):
            if not in_ul:
                out.append("<ol>")
                in_ul = True  # reuse flag
            out.append(f"<li>{_inline_md(_esc(re.sub(r'^\d+\.\s+', '', line)))}</li>")
            continue

        # Close list if needed
        if in_ul and line.strip() == "":
            out.append("</ul>")
            in_ul = False

        if line.strip() == "":
            out.append("")
        else:
            out.append(f"<p>{_inline_md(_esc(line))}</p>")

    if in_table:
        out.append("</table>")
    if in_ul:
        out.append("</ul>")
    if in_code:
        out.append("</pre></code>")

    return "\n".join(out)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_md(s: str) -> str:
    """Apply inline Markdown/mrkdwn: bold, italic, code, emoji shortcuts."""
    # Emoji text aliases
    s = s.replace(":white_check_mark:", "✅").replace(":warning:", "⚠️")
    s = s.replace(":red_circle:", "🔴").replace(":large_yellow_circle:", "🟡")
    # Bold **text** or *text* (mrkdwn)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*(.+?)\*", r"<strong>\1</strong>", s)
    # Italic _text_
    s = re.sub(r"_(.+?)_", r"<em>\1</em>", s)
    # Inline code
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    # Links [text](url)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    return s


# ---------------------------------------------------------------------------
# Board status table from signals
# ---------------------------------------------------------------------------

def _board_status_rows(signals: dict) -> str:
    sprints = signals.get("all_sprints", [])
    issues = signals.get("issues", [])
    today = date.today()

    # Group issues by sprint id if issues carry sprint info; otherwise use totals
    done_statuses = {"done", "closed", "resolved", "won't fix", "wont fix", "rejected"}

    total = len(issues)
    done_n = sum(1 for i in issues if i.get("status", "").lower() in done_statuses)
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
# Per-person table
# ---------------------------------------------------------------------------

def _person_rows(signals: dict) -> str:
    issues = signals.get("issues", [])
    done_statuses = {"done", "closed", "resolved", "won't fix", "wont fix", "rejected"}
    by_person: dict[str, dict] = defaultdict(lambda: {"done": [], "active": [], "stale": []})

    for i in issues:
        person = i.get("assignee", "Unassigned")
        status = i.get("status", "").lower()
        key = i.get("key", "")
        summary = i.get("summary", "")
        url = i.get("url", "")
        label = f'<a href="{_esc(url)}">{_esc(key)}</a>' if url else _esc(key)
        entry = f'{label} <span class="text-muted small">{_esc(summary[:60])}</span>'
        if i.get("is_stale"):
            by_person[person]["stale"].append(entry)
        elif status in done_statuses:
            by_person[person]["done"].append(entry)
        else:
            by_person[person]["active"].append(entry)

    if not by_person:
        return "<tr><td colspan='4' class='text-muted'>No issues found</td></tr>"

    rows = ""
    for person, buckets in sorted(by_person.items()):
        done_html = "<br>".join(buckets["done"]) or '<span class="text-muted">—</span>'
        active_html = "<br>".join(buckets["active"]) or '<span class="text-muted">—</span>'
        stale_badge = ""
        if buckets["stale"]:
            stale_html = "<br>".join(buckets["stale"])
            stale_badge = f'<br><span class="badge bg-warning text-dark">⚠️ stale</span> {stale_html}'
        rows += (
            f"<tr><td><strong>{_esc(person)}</strong></td>"
            f"<td>{done_html}</td>"
            f"<td>{active_html}{stale_badge}</td>"
            f"<td class='text-center'>{len(buckets['done'])}/{len(issues)}</td></tr>"
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

<!-- ── Per-person Status ─────────────────────────────────────────────────── -->
<h3>Per-person Status</h3>
<table class="table table-bordered table-sm table-hover">
  <thead class="table-secondary">
    <tr>
      <th style="width:14%">Person</th>
      <th style="width:30%">Completed</th>
      <th>In Progress / In Review</th>
      <th class="text-center" style="width:8%">Done/Total</th>
    </tr>
  </thead>
  <tbody>
    {person_rows}
  </tbody>
</table>

<!-- ── Risk Report ───────────────────────────────────────────────────────── -->
<h3>Risk Report</h3>
<div class="risk-detail">
  {risk_html}
</div>

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
    risk_md = risk_report_path.read_text() if risk_report_path.exists() else ""
    risk_html = _md_to_html(risk_md) if risk_md else "<p class='text-muted'>No risk report generated.</p>"

    report_date = date.today().isoformat()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = _HTML.format(
        report_date=report_date,
        generated_at=generated_at,
        board_rows=_board_status_rows(signals),
        person_rows=_person_rows(signals),
        risk_html=risk_html,
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
