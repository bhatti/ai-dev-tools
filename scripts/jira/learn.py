"""Extract learnings and PR health analysis from a completed BitBucket PR lifecycle.

Usage:
    python -m scripts.jira.learn --issue-id PROJ-42

Required env: BITBUCKET_USERNAME, BITBUCKET_TOKEN (or from pr.json)
Reads:  /workspace/{issue_id}/pr.json
        /workspace/{issue_id}/impl_result.json
Writes: /workspace/{issue_id}/learnings.md
Posts:  Comment to Bitbucket PR and Jira issue

Exit codes: 0=done, 1=error
"""

import json
import sys

import click
import requests as _requests

from scripts.analyze.pr_fetcher import (
    build_pr_context, fetch_issue_details, fetch_single_pr, link_pr_to_issue,
)
from scripts.common.artifacts import read_json, write_text
from scripts.common.bitbucket_api import add_pr_comment, list_pr_comments
from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS
from scripts.common.config import get_issue_dir, load_config, validate_claude_config
from scripts.common.learn_prompts import JIRA_LEARN_PROMPT_TEMPLATE as LEARN_PROMPT_TEMPLATE


def _post_jira_comment(config: dict, issue_key: str, body: str) -> None:
    """Post a comment to a Jira issue. Non-fatal — logs warning on failure."""
    jira_url = config.get("JIRA_BASE_URL", "").rstrip("/")
    email = config.get("JIRA_EMAIL", "")
    token = config.get("JIRA_API_TOKEN", "")
    if not all([jira_url, email, token, issue_key]):
        return
    try:
        _requests.post(
            f"{jira_url}/rest/api/2/issue/{issue_key}/comment",
            auth=(email, token),
            json={"body": body[:30000]},
            timeout=15,
        )
    except Exception as e:
        print(f"[learn] WARNING: could not post Jira comment: {e}", file=sys.stderr, flush=True)


def _post_bb_comment(config: dict, workspace: str, repo_name: str, pr_id, report: str) -> None:
    """Post report to a BB PR comment. Non-fatal — logs warning on failure."""
    if not workspace or not repo_name or not pr_id:
        return
    try:
        add_pr_comment(config, workspace, repo_name, pr_id, report[:8000])
    except Exception as e:
        print(f"[learn] WARNING: could not post BB PR comment: {e}", file=sys.stderr, flush=True)


@click.command()
@click.option("--issue-id", required=True, help="Jira issue key (e.g. PROJ-42)")
def main(issue_id: str) -> None:
    config = load_config(required=[])
    validate_claude_config(config)
    print(f"[learn] issue={issue_id}", flush=True)

    pr = read_json(config, issue_id, "pr.json")
    issue = read_json(config, issue_id, "issue.json")
    impl_result = read_json(config, issue_id, "impl_result.json") or {}

    if not pr:
        print("ERROR: Missing pr.json", file=sys.stderr)
        sys.exit(1)

    workspace = pr.get("workspace") or config.get("BITBUCKET_WORKSPACE", "")
    repo_name = pr.get("repo") or config.get("BITBUCKET_REPO", "")
    pr_id = (pr.get("number") or pr.get("id") or pr.get("url", "").rstrip("/").split("/")[-1])
    issue_dir = get_issue_dir(config, issue_id)

    # Fetch single-PR data for health check context (non-fatal if unavailable)
    try:
        pr_data = fetch_single_pr(config, int(pr_id))
        if pr_data:
            issue_ref = link_pr_to_issue(pr_data, config)
            if issue_ref:
                pr_data["linked_issue"] = issue_ref
                pr_data["linked_issue"]["details"] = fetch_issue_details(issue_ref, config)
            pr_context = build_pr_context([pr_data], max_chars=8000)
        else:
            pr_context = "(PR data unavailable — using comment-only analysis)"
    except Exception as e:
        print(f"[learn] WARNING: fetch_single_pr failed (non-fatal): {e}", file=sys.stderr, flush=True)
        pr_context = "(PR data unavailable — using comment-only analysis)"

    comments = list_pr_comments(config, workspace, repo_name, pr_id)
    comments_text = "\n\n".join(
        f"@{c.get('author', {}).get('nickname', 'unknown')}: "
        f"{c.get('content', {}).get('raw', '') or c.get('body', '')}"
        for c in comments
    ) or "(no comments)"

    # pr-audit workflow has no issue.json — build context from audit reports instead
    if not issue:
        from scripts.common.config import get_workspace_dir
        workspace_dir = get_workspace_dir(config)
        audit_report = ""
        for path in [
            workspace_dir / "reports" / "pr_audit_report.md",
            workspace_dir / "reports" / "skill_update_plan.md",
        ]:
            if path.exists():
                audit_report += f"\n\n## {path.name}\n" + path.read_text(encoding="utf-8")[:2000]
        impl_summary = audit_report or json.dumps(impl_result, indent=2)
        title = f"PR audit skill improvements — {pr.get('url', pr_id)}"
        issue_id_label = "pr-audit"
    else:
        impl_summary = json.dumps(impl_result, indent=2)
        title = issue["title"]
        issue_id_label = issue_id

    prompt = LEARN_PROMPT_TEMPLATE.format(
        issue_id=issue_id_label,
        title=title,
        pr_context=pr_context,
        impl_summary=impl_summary,
        comments_text=comments_text,
    )

    result = run_claude(
        prompt,
        working_dir=issue_dir,
        model=config.get("AI_MODEL"),
        max_turns=int(config.get("MAX_TURNS_LEARN", "30")),
        log_file=issue_dir / "logs" / "learn.log",
        system_prompt=SYSTEM_PROMPTS["learn"],
    )

    lines = result.output.strip().splitlines()
    json_start = next(
        (i for i, l in enumerate(lines) if l.strip().startswith('{"status"')), len(lines)
    )
    learnings_content = "\n".join(lines[:json_start]).strip()

    if learnings_content:
        write_text(config, issue_id, "learnings.md", learnings_content)
        print(f"Learnings written to workspace/{issue_id}/learnings.md")

    # Post combined report to BB PR and Jira issue (non-fatal)
    learnings_path = issue_dir / "learnings.md"
    if learnings_path.exists():
        report = learnings_path.read_text(encoding="utf-8")
        _post_bb_comment(config, workspace, repo_name, pr_id, report)
        if issue:
            _post_jira_comment(config, issue_id, report)

    print(f"Learn complete: {result.status_json}")
    sys.exit(0)


if __name__ == "__main__":
    main()
