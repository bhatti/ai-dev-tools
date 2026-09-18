"""Markdown → Slack mrkdwn conversion and text-limit helpers.

Shared by all Slack-posting scripts (post_audit, post_pr_audit, standup post).
"""

from __future__ import annotations

import re

SLACK_TEXT_LIMIT = 38_000


def build_md_report_header(title: str, repo: str, branch: str,
                            meta_items: list[str], summary_line: str) -> str:
    """Return a markdown metadata block to prepend to HTML/MD audit report artifacts.

    Produces:
        # {title} — {repo} @ {branch}

        {meta_items joined with  ·  }
        {summary_line}

        ---

    """
    heading = f"# {title} — {repo}" + (f" @ {branch}" if branch else "")
    parts = [heading, "", "  ·  ".join(meta_items), summary_line, "", "---", ""]
    return "\n".join(parts)


def is_full_report(config: dict) -> bool:
    """Return True if --full was requested via env var (API path) or SLACK_MESSAGE (Slack path).

    run_*_audit.py sets AUDIT_FULL_REPORT=1 in its own process, but that env var dies with
    the subprocess and is not inherited by the parent bash shell that later runs the post script.
    SLACK_MESSAGE is exported by the bash routing layer and IS inherited, so we check both.
    """
    if config.get("AUDIT_FULL_REPORT", "").strip() == "1":
        return True
    return bool(re.search(r"--full\b", config.get("SLACK_MESSAGE", ""), re.IGNORECASE))


def format_for_slack(text: str) -> str:
    """Convert markdown to Slack mrkdwn format."""
    # Fenced code blocks → plain indented text (Slack supports ```code``` blocks)
    text = re.sub(r'```[^\n]*\n(.*?)```', lambda m: '```' + m.group(1).rstrip() + '```',
                  text, flags=re.DOTALL)
    # Markdown tables → plain aligned text (Slack doesn't render tables)
    def _table_to_text(m: re.Match) -> str:
        rows = []
        for row in m.group(0).splitlines():
            # Skip separator rows (|---|---|)
            if re.match(r'^\|[\s\-\|:]+\|?\s*$', row):
                continue
            cells = [c.strip() for c in row.strip('|').split('|')]
            rows.append('  '.join(c for c in cells if c))
        return '\n'.join(rows)
    text = re.sub(r'(\|[^\n]+\n)+', _table_to_text, text)
    # Headings → bold
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    # Bold **text** → *text*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # __bold__ → *bold*
    text = re.sub(r'__(.+?)__', r'*\1*', text)
    # [text](url) → <url|text>
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)
    # Horizontal rules → empty line
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    # Bullet lists
    text = re.sub(r'^[ \t]*[-*+][ \t]+', '• ', text, flags=re.MULTILINE)
    # Numbered lists
    text = re.sub(r'^[ \t]*\d+\.[ \t]+', '• ', text, flags=re.MULTILINE)
    # Collapse 3+ blank lines → 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    if len(text) > SLACK_TEXT_LIMIT:
        text = text[:SLACK_TEXT_LIMIT] + "\n…(truncated — see full report in job artifacts)"
    return text


_SECTION_HEADING_FIRST_LINE = re.compile(
    r"^#{1,6}\s+(?:standup_brief|standup brief|risk_report|risk report|standup_report|standup report|full risk report|full_risk_report)",
    re.IGNORECASE,
)


def strip_section_heading(text: str) -> str:
    """Remove the first line if it is a Markdown section heading for a known standup section.

    Handles headings with extra detail, e.g.:
      '# Risk Report — DistMgmt Sprint 202 — 2026-09-18'
    Only the first line is inspected so body headings are never accidentally removed.
    """
    text = text.strip()
    first, _, rest = text.partition("\n")
    if _SECTION_HEADING_FIRST_LINE.match(first):
        return rest.strip()
    return text


def build_artifact_links(config: dict, task_type: str, report_filename: str) -> tuple[str, str]:
    """Return (html_url, job_url) for Slack artifact links.

    html_url uses the by-job endpoint with task filter to extract the HTML
    report directly from the correct task's artifact zip.
    job_url links to the job page which has the full zip download.
    Returns ("", "") when FORMICARY_PUBLIC_URL or JOB_ID are not set.
    """
    base = (config.get("FORMICARY_PUBLIC_URL", "") or "").rstrip("/")
    job_id = config.get("JOB_ID", "") or ""
    if not base or not job_id:
        return "", ""
    html_url = f"{base}/dashboard/artifacts/by-job/{job_id}/download?task={task_type}&file=reports/{report_filename}"
    job_url = f"{base}/dashboard/jobs/requests/{job_id}"
    return html_url, job_url
