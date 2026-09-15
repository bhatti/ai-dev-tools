"""Markdown → Slack mrkdwn conversion and text-limit helpers.

Extracted from post_audit.py so that both post_audit and post_pr_audit
(and any future Slack-posting scripts) share the same formatting logic.
"""

from __future__ import annotations

import re

SLACK_TEXT_LIMIT = 38_000


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
