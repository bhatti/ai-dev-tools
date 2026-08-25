"""Apply human feedback after PR review pause.

Flow (signal-based, two possible pauses):

  DECISION=approve
      → gh pr review --approve  (or Bitbucket equivalent)
      → Slack confirm
      → exit 0  (→ done)

  DECISION=request-changes  (no REPLY_TEXT yet)
      → format draft review with inline file:line comments
      → post draft to Slack thread asking human to edit / confirm
      → exit 3  (→ PAUSE_JOB for human edit)

  DECISION=request-changes  (REPLY_TEXT set by thread reply)
      → parse any edits from ReplyText
      → post inline review to GitHub / Bitbucket PR
      → Slack confirmation
      → exit 0  (→ done)

  DECISION=verify
      → run Claude to re-verify each finding against the actual file
      → update findings.json with verified subset
      → re-post updated Block Kit to Slack
      → exit 3  (→ PAUSE_JOB — human gets a fresh Approve/Request Changes choice)

Required env:
    DECISION        (approve | request-changes | verify)
Optional env:
    REPLY_TEXT      set by Slack router on thread reply (empty on first run)
    SLACK_BOT_TOKEN, SLACK_CHANNEL, SLACK_THREAD_TS, JOB_ID
    GH_TOKEN        (for GitHub PR posting)
    BITBUCKET_USERNAME, BITBUCKET_TOKEN  (for Bitbucket PR posting)

Reads:  findings.json, DECISION env var
Writes: /workspace/apply_result.json, /workspace/findings.json (on verify)

Exit codes: 0=done, 1=error, 3=PAUSE_JOB (waiting for human input)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import requests

from scripts.common.config import get_workspace_dir, load_config

# --------------------------------------------------------------------- helpers

_SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
_CONFIRM_WORDS = frozenset({"post it", "confirm", "ok", "yes", "send", "lgtm", "ship it", "post"})


def _is_github_url(url: str) -> bool:
    return "github.com" in url.lower()


def _is_bitbucket_url(url: str) -> bool:
    return "bitbucket.org" in url.lower()


def _parse_github_pr(url: str) -> tuple[str, str, str] | None:
    """Return (owner, repo, pr_number) or None."""
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url, re.I)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def _parse_bitbucket_pr(url: str) -> tuple[str, str, str] | None:
    """Return (workspace, repo, pr_id) or None."""
    m = re.search(r"bitbucket\.org/([^/]+)/([^/]+)/pull-requests/(\d+)", url, re.I)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def _slack_post(token: str, channel: str, thread_ts: str | None, text: str) -> bool:
    if not token or not channel:
        return False
    payload: dict = {
        "channel": channel.lstrip("#"),
        "text": text,
        "unfurl_links": False,
        "mrkdwn": True,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    if not resp.ok:
        print(f"[apply_feedback] Slack HTTP {resp.status_code}", file=sys.stderr, flush=True)
        return False
    data = resp.json()
    if not data.get("ok"):
        print(f"[apply_feedback] Slack error: {data.get('error')}", file=sys.stderr, flush=True)
    return data.get("ok", False)


def _slack_block_kit(token: str, channel: str, thread_ts: str | None, blocks: list) -> bool:
    if not token or not channel:
        return False
    payload: dict = {
        "channel": channel.lstrip("#"),
        "blocks": blocks,
        "unfurl_links": False,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    if not resp.ok:
        return False
    return resp.json().get("ok", False)


def _build_repost_blocks(findings: dict, job_id: str) -> list:
    """Block Kit for re-posting after verify (same buttons, updated counts)."""
    n = len(findings.get("findings", []))
    verdict = findings.get("verdict", "COMMENT")
    summary = findings.get("summary", "")
    emoji = {"APPROVE": "✅", "REQUEST_CHANGES": "🔄", "COMMENT": "💬"}.get(verdict, "💬")
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} Updated Review — {verdict}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Verified findings: {n}*\n{summary}"}},
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve"},
                 "style": "primary", "action_id": "review_decision", "value": f"{job_id}:approve"},
                {"type": "button", "text": {"type": "plain_text", "text": "🔄 Request Changes"},
                 "style": "danger", "action_id": "review_decision", "value": f"{job_id}:request-changes"},
            ],
        },
    ]


# --------------------------------------------------------------------- review body formatting

def _format_review_body(findings: dict, edits: str = "") -> str:
    """Format findings as a GitHub/Bitbucket review comment body (Markdown)."""
    if edits and edits.lower().strip() not in _CONFIRM_WORDS:
        # Human provided actual edit text — use it verbatim
        return edits.strip()

    verdict = findings.get("verdict", "COMMENT")
    summary = findings.get("summary", "")
    lines: list[str] = [f"## PR Review\n\n**Verdict:** {verdict}\n\n{summary}\n"]

    sorted_findings = sorted(
        findings.get("findings", []),
        key=lambda f: _SEVERITY_RANK.get(f.get("severity", "LOW"), 3),
    )

    current_severity = None
    for f in sorted_findings:
        sev = f.get("severity", "LOW").upper()
        if sev != current_severity:
            current_severity = sev
            lines.append(f"\n### {sev}\n")
        file_ref = f.get("file", "")
        line = f.get("line")
        location = f"`{file_ref}:{line}`" if file_ref and line else f"`{file_ref}`" if file_ref else ""
        title = f.get("title", "")
        description = f.get("description", "")
        fix_text = f.get("fix", "")
        lines.append(f"**{title}** {location}\n{description}")
        if fix_text:
            lines.append(f"_Suggested fix: {fix_text}_")
        lines.append("")

    lines.append(f"\n_ai-bot review — reply with `ai-bot <instruction>` to request changes._")
    return "\n".join(lines)


def _format_draft_for_slack(findings: dict) -> str:
    """Short human-readable draft for Slack preview (max ~2000 chars)."""
    body = _format_review_body(findings)
    if len(body) > 2000:
        body = body[:1950] + "\n…_(truncated — full review will be posted to the PR)_"
    return body


# --------------------------------------------------------------------- GitHub PR actions

def _gh_approve(owner: str, repo: str, pr_num: str, summary: str) -> bool:
    cmd = [
        "gh", "pr", "review", pr_num,
        "--repo", f"{owner}/{repo}",
        "--approve",
        "--body", summary or "Approved.",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"[apply_feedback] gh pr approve failed: {result.stderr}", file=sys.stderr, flush=True)
        return False
    print(f"[apply_feedback] PR {owner}/{repo}#{pr_num} approved via gh", flush=True)
    return True


def _gh_request_changes(
    owner: str, repo: str, pr_num: str,
    review_body: str, findings: list,
    gh_token: str,
) -> bool:
    """Post a review with request-changes to GitHub, adding inline file:line comments."""
    # Build inline comments list for the API call
    comments = []
    for f in findings:
        file_path = f.get("file", "")
        line = f.get("line")
        if not file_path or not line:
            continue  # no inline anchor — captured in body instead
        try:
            line_int = int(line)
        except (TypeError, ValueError):
            continue
        comment_body = f"**{f.get('severity','?')}** — {f.get('title','')}\n{f.get('description','')}"
        if f.get("fix"):
            comment_body += f"\n_Fix: {f['fix']}_"
        comments.append({"path": file_path, "line": line_int, "body": comment_body})

    payload: dict[str, Any] = {
        "body": review_body,
        "event": "REQUEST_CHANGES",
        "comments": comments,
    }

    resp = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}/reviews",
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        print(f"[apply_feedback] GitHub review API {resp.status_code}: {resp.text[:300]}", file=sys.stderr, flush=True)
        return False
    print(f"[apply_feedback] GitHub review posted (id={resp.json().get('id')})", flush=True)
    return True


# --------------------------------------------------------------------- Bitbucket PR actions

def _bb_post_review(
    workspace: str, repo: str, pr_id: str,
    review_body: str, findings: list,
    bb_user: str, bb_token: str,
) -> bool:
    """Post a comment-based review to Bitbucket (no formal review API — uses PR comments)."""
    auth = (bb_user, bb_token)
    base = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}/pullrequests/{pr_id}/comments"

    # Post overall summary as one comment
    resp = requests.post(
        base,
        auth=auth,
        json={"content": {"raw": review_body}},
        timeout=30,
    )
    if not resp.ok:
        print(f"[apply_feedback] Bitbucket overall comment {resp.status_code}: {resp.text[:200]}", file=sys.stderr, flush=True)
        return False

    # Post inline comments for findings with file:line
    for f in findings:
        file_path = f.get("file", "")
        line = f.get("line")
        if not file_path or not line:
            continue
        try:
            line_int = int(line)
        except (TypeError, ValueError):
            continue
        inline_body = f"**{f.get('severity','?')}** — {f.get('title','')}\n{f.get('description','')}"
        if f.get("fix"):
            inline_body += f"\n_Fix: {f['fix']}_"
        inline_resp = requests.post(
            base,
            auth=auth,
            json={"content": {"raw": inline_body}, "inline": {"to": line_int, "path": file_path}},
            timeout=30,
        )
        if not inline_resp.ok:
            print(
                f"[apply_feedback] Bitbucket inline comment failed ({file_path}:{line}): {inline_resp.status_code}",
                file=sys.stderr, flush=True,
            )
    print(f"[apply_feedback] Bitbucket review posted to {workspace}/{repo}#{pr_id}", flush=True)
    return True


# --------------------------------------------------------------------- verify flow

def _verify_findings(findings: dict, workspace: Path, config: dict) -> dict:
    """Run Claude to re-verify each finding against actual file content.

    Claude is instructed to re-read each flagged file, confirm whether the
    finding is real (removing false positives), then rewrite findings.json.
    Returns the (possibly updated) findings dict.
    """
    try:
        from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS  # noqa: PLC0415
    except ImportError:
        print("[apply_feedback] claude_runner not available — skipping verify", flush=True)
        return findings

    sorted_findings = sorted(
        findings.get("findings", []),
        key=lambda f: _SEVERITY_RANK.get(f.get("severity", "LOW"), 3),
    )

    # Build a compact list of claims for Claude to verify
    claims = "\n".join(
        f"[{i+1}] {f.get('severity')} {f.get('file','')}:{f.get('line','')} — {f.get('title','')} — {f.get('description','')}"
        for i, f in enumerate(sorted_findings[:20])  # cap at 20 findings for speed
    )

    prompt = f"""\
You are verifying PR review findings to remove false positives before posting to the PR.

For each finding below, read the actual file at the given path and line number in the
current working directory, then decide: KEEP (real issue) or DROP (false positive).

PR URL: {findings.get('pr_url', '')}

Findings to verify:
{claims}

For each finding:
1. Read the relevant source file (use cat/grep to check the exact code at that line).
2. Check whether the issue is real or a hallucination.
3. If real: keep it. If false positive or not reproducible: drop it.

After verifying ALL findings, rewrite findings.json in the current directory with
only the KEPT findings, updating the verdict and summary accordingly.

Output ONLY this JSON on the last line:
{{"status":"DONE","kept":<N>,"dropped":<M>,"verdict":"<APPROVE|REQUEST_CHANGES|COMMENT>","summary":"<one sentence>"}}
"""

    logs_dir = workspace / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = run_claude(
            prompt,
            working_dir=workspace,
            model=config.get("AI_MODEL"),
            max_turns=int(config.get("MAX_TURNS_IMPLEMENT", "40")),
            log_file=logs_dir / "verify.log",
            system_prompt=SYSTEM_PROMPTS["respond"],
        )
    except RuntimeError as e:
        print(f"[apply_feedback] verify claude error: {e}", file=sys.stderr, flush=True)
        return findings

    # Reload findings.json in case Claude updated it
    findings_path = workspace / "findings.json"
    if findings_path.exists():
        try:
            updated = json.loads(findings_path.read_text(encoding="utf-8"))
            status_data = result.status_json or {}
            kept = status_data.get("kept", "?")
            dropped = status_data.get("dropped", "?")
            print(f"[apply_feedback] verify: kept={kept} dropped={dropped}", flush=True)
            return updated
        except (json.JSONDecodeError, OSError):
            pass
    return findings


# --------------------------------------------------------------------- main

@click.command()
@click.option("--findings", "findings_path", default="/workspace/findings.json", show_default=True,
              help="Path to findings.json written by run.py")
def main(findings_path: str) -> None:  # noqa: C901 (complexity: branching by design)
    config = load_config()

    decision = config.get("DECISION", "").strip().lower()
    reply_text = config.get("REPLY_TEXT", "").strip()

    if not decision:
        print("[apply_feedback] DECISION env var not set", file=sys.stderr, flush=True)
        sys.exit(1)

    token = config.get("SLACK_BOT_TOKEN", "")
    channel = config.get("SLACK_CHANNEL", "")
    thread_ts = config.get("SLACK_THREAD_TS", "") or None
    job_id = config.get("JOB_ID", "unknown")

    # Read findings
    fpath = Path(findings_path)
    findings: dict = {"pr_url": "", "verdict": "COMMENT", "findings": [], "summary": ""}
    if fpath.exists():
        try:
            findings = json.loads(fpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[apply_feedback] cannot read findings: {e}", file=sys.stderr, flush=True)

    pr_url = findings.get("pr_url", "")
    summary = findings.get("summary", "")
    finding_list = findings.get("findings", [])
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"[apply_feedback] decision={decision} reply_text={reply_text[:80]!r} pr_url={pr_url}", flush=True)

    # ------------------------------------------------------------------ VERIFY
    if decision == "verify":
        _slack_post(token, channel, thread_ts,
                    "🔍 Verifying findings against actual source — re-reading flagged files…")
        updated_findings = _verify_findings(findings, workspace, config)
        n = len(updated_findings.get("findings", []))
        # Persist updated findings
        fpath.write_text(json.dumps(updated_findings, indent=2), encoding="utf-8")
        _slack_post(token, channel, thread_ts, f"✅ Verification done — {n} finding(s) confirmed real.")
        # Re-post Block Kit so human can now Approve or Request Changes
        blocks = _build_repost_blocks(updated_findings, job_id)
        _slack_block_kit(token, channel, thread_ts, blocks)
        _write_result(workspace, {"status": "VERIFY_DONE", "verified_count": n, "decision": decision})
        sys.exit(3)  # PAUSE — human will click Approve or Request Changes on updated Block Kit

    # ------------------------------------------------------------------ APPROVE
    if decision == "approve":
        pr_posted = False
        if pr_url and _is_github_url(pr_url):
            parsed = _parse_github_pr(pr_url)
            if parsed:
                owner, repo, pr_num = parsed
                gh_token = config.get("GH_TOKEN", "")
                if gh_token:
                    pr_posted = _gh_approve(owner, repo, pr_num, summary)
                else:
                    print("[apply_feedback] GH_TOKEN not set — cannot post to GitHub PR", file=sys.stderr, flush=True)
        elif pr_url and _is_bitbucket_url(pr_url):
            # Bitbucket doesn't have a formal "approve PR" API in the same way;
            # post a comment indicating approval.
            parsed_bb = _parse_bitbucket_pr(pr_url)
            if parsed_bb:
                workspace_bb, repo_bb, pr_id_bb = parsed_bb
                bb_user = config.get("BITBUCKET_USERNAME", "")
                bb_token = config.get("BITBUCKET_TOKEN", "")
                if bb_user and bb_token:
                    resp = requests.post(
                        f"https://api.bitbucket.org/2.0/repositories/{workspace_bb}/{repo_bb}/pullrequests/{pr_id_bb}/comments",
                        auth=(bb_user, bb_token),
                        json={"content": {"raw": f"✅ Approved — {summary}"}},
                        timeout=30,
                    )
                    pr_posted = resp.ok
        pr_note = " (PR updated)" if pr_posted else " (could not post to PR — GH_TOKEN/BB_TOKEN missing?)"
        _slack_post(token, channel, thread_ts, f"✅ *Approved*{pr_note}\n_{summary}_")
        _write_result(workspace, {"status": "DONE", "decision": "approve", "pr_url": pr_url, "pr_posted": pr_posted})
        sys.exit(0)

    # ------------------------------------------------------------------ REQUEST-CHANGES
    if decision == "request-changes":
        # First run: no reply_text → post draft to Slack, PAUSE for human review
        if not reply_text:
            draft = _format_draft_for_slack(findings)
            n_findings = len(finding_list)
            _slack_post(
                token, channel, thread_ts,
                f"📝 *Draft PR review ({n_findings} finding(s)):*\n\n{draft}\n\n"
                f"Reply `post it` to publish as-is, or reply with your edited version.",
            )
            _write_result(workspace, {"status": "DRAFT_POSTED", "decision": decision})
            sys.exit(3)  # PAUSE — wait for human edit / confirm

        # Second run: reply_text is set — post to actual PR then confirm
        review_body = _format_review_body(findings, edits=reply_text)
        pr_posted = False

        if pr_url and _is_github_url(pr_url):
            parsed = _parse_github_pr(pr_url)
            if parsed:
                owner, repo, pr_num = parsed
                gh_token = config.get("GH_TOKEN", "")
                if gh_token:
                    pr_posted = _gh_request_changes(owner, repo, pr_num, review_body, finding_list, gh_token)
                else:
                    print("[apply_feedback] GH_TOKEN not set — cannot post inline review to PR", file=sys.stderr, flush=True)
        elif pr_url and _is_bitbucket_url(pr_url):
            parsed_bb = _parse_bitbucket_pr(pr_url)
            if parsed_bb:
                workspace_bb, repo_bb, pr_id_bb = parsed_bb
                bb_user = config.get("BITBUCKET_USERNAME", "")
                bb_token = config.get("BITBUCKET_TOKEN", "")
                if bb_user and bb_token:
                    pr_posted = _bb_post_review(workspace_bb, repo_bb, pr_id_bb, review_body, finding_list, bb_user, bb_token)

        pr_note = " (posted to PR with inline comments)" if pr_posted else " (Slack only — PR token missing)"
        _slack_post(token, channel, thread_ts, f"🔄 *Changes Requested*{pr_note}\n_{summary}_")
        _write_result(workspace, {
            "status": "DONE", "decision": decision, "pr_url": pr_url,
            "pr_posted": pr_posted, "inline_count": len([f for f in finding_list if f.get("file") and f.get("line")]),
        })
        sys.exit(0)

    # Unknown decision
    print(f"[apply_feedback] unknown DECISION={decision!r}", file=sys.stderr, flush=True)
    sys.exit(1)


def _write_result(workspace: Path, data: dict) -> None:
    p = workspace / "apply_result.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
