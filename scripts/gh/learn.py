"""Extract learnings and PR health analysis from a completed GitHub PR lifecycle.

Usage:
    python -m scripts.gh.learn --issue-id 42

Required env: GH_ORG, GH_REPO, GH_TOKEN
Reads:  /workspace/pr.json
        /workspace/impl_result.json
Writes: /workspace/learnings.md
Posts:  Comment to GH PR and GH issue

Exit codes: 0=done, 1=error
"""

import json
import subprocess
import sys

import click

from scripts.analyze.pr_fetcher import (
    build_pr_context, fetch_issue_details, fetch_single_pr, link_pr_to_issue,
)
from scripts.common.artifacts import read_json, read_text, write_json, write_text
from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS
from scripts.common.config import get_issue_dir, load_config, validate_claude_config
from scripts.common.learn_prompts import GH_LEARN_PROMPT_TEMPLATE as LEARN_PROMPT_TEMPLATE
from scripts.common.shell import run_cmd as _run


def fetch_pr_comments(org: str, repo: str, pr_number: int) -> list[dict]:
    """Fetch all comments from the PR. Raises on API failure."""
    comments = []
    for endpoint in [
        f"repos/{org}/{repo}/issues/{pr_number}/comments",
        f"repos/{org}/{repo}/pulls/{pr_number}/comments",
    ]:
        result = _run(["gh", "api", endpoint], check=False)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to fetch comments from {endpoint}: {result.stderr.strip()}")
        comments.extend(json.loads(result.stdout or "[]"))
    return comments


def _post_gh_comment(resource: str, number: int | str, org: str, repo: str, body: str) -> None:
    """Post a comment to a GH PR or issue. resource = 'pr' or 'issue'. Non-fatal."""
    if not org or not repo or not number:
        return
    cmd = ["gh", resource, "comment", str(number), "-R", f"{org}/{repo}", "--body", body[:8000]]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(
                f"[learn] WARNING: could not post GH {resource} comment: {result.stderr.strip()[:200]}",
                file=sys.stderr, flush=True,
            )
    except Exception as e:
        print(f"[learn] WARNING: GH comment failed (non-fatal): {e}", file=sys.stderr, flush=True)


@click.command()
@click.option("--issue-id", required=True, help="Issue number")
def main(issue_id: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO", "GH_TOKEN"])
    validate_claude_config(config)
    print(f"[learn] issue={issue_id} org={config['GH_ORG']} repo={config['GH_REPO']}", flush=True)

    pr = read_json(config, issue_id, "pr.json")
    issue = read_json(config, issue_id, "issue.json")
    impl_result = read_json(config, issue_id, "impl_result.json") or {}

    if not pr or not issue:
        print("ERROR: Missing pr.json or issue.json", file=sys.stderr)
        sys.exit(1)

    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    pr_number = int(pr["number"])
    issue_dir = get_issue_dir(config, issue_id)

    # Fetch single-PR data for health check context (non-fatal if unavailable)
    try:
        pr_data = fetch_single_pr(config, pr_number)
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

    comments = fetch_pr_comments(org, repo, pr_number)
    comments_text = "\n\n".join(
        f"@{c.get('user', {}).get('login', 'unknown')}: {c.get('body', '')}"
        for c in comments
    ) or "(no comments)"

    prompt = LEARN_PROMPT_TEMPLATE.format(
        issue_id=issue_id,
        title=issue["title"],
        pr_context=pr_context,
        impl_summary=json.dumps(impl_result, indent=2),
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

    # Extract any markdown content from output (before the final JSON)
    lines = result.output.strip().splitlines()
    json_start = next(
        (i for i, l in enumerate(lines) if l.strip().startswith('{"status"')), len(lines)
    )
    learnings_content = "\n".join(lines[:json_start]).strip()

    if learnings_content:
        write_text(config, issue_id, "learnings.md", learnings_content)
        print(f"Learnings written to workspace/{issue_id}/learnings.md")

    # Post combined report to GH PR and GH issue (non-fatal)
    learnings_path = issue_dir / "learnings.md"
    if learnings_path.exists():
        report = learnings_path.read_text(encoding="utf-8")
        _post_gh_comment("pr", pr_number, org, repo, report)
        _post_gh_comment("issue", issue_id, org, repo, report)

    print(f"Learn complete: {result.status_json}")
    sys.exit(0)


if __name__ == "__main__":
    main()
