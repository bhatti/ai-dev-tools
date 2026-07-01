"""Extract learnings from a completed BitBucket PR lifecycle.

Usage:
    python -m scripts.jira.learn --issue-id PROJ-42

Required env: BITBUCKET_USERNAME, BITBUCKET_TOKEN (or from pr.json)
Reads:  /workspace/{issue_id}/pr.json
        /workspace/{issue_id}/impl_result.json
Writes: /workspace/{issue_id}/learnings.md

Exit codes: 0=done, 1=error
"""

import json
import sys

import click

from scripts.common.artifacts import read_json, write_text
from scripts.common.bitbucket_api import list_pr_comments
from scripts.common.claude_runner import run_claude
from scripts.common.config import get_issue_dir, load_config, validate_claude_config


LEARN_PROMPT_TEMPLATE = """\
You are an expert software engineer extracting learnings from a completed AI coding session.

## {issue_id}: {title}

## Implementation Summary
{impl_summary}

## PR Comments
{comments_text}

## Instructions
Use the /ygs-learn skill to extract actionable learnings:
1. What patterns worked well?
2. What conventions or project-specific patterns were encountered?
3. What should be done differently next time?
4. Any surprises or non-obvious findings?

Write the learnings as a markdown document suitable for reference in future sessions.

Output ONLY this JSON on the last line:
{{"status":"DONE","learning_count":<N>}}
"""


@click.command()
@click.option("--issue-id", required=True, help="Jira issue key (e.g. PROJ-42)")
def main(issue_id: str) -> None:
    config = load_config(required=[])
    validate_claude_config(config)
    print(f"[learn] issue={issue_id}", flush=True)

    pr = read_json(config, issue_id, "pr.json")
    issue = read_json(config, issue_id, "issue.json")
    impl_result = read_json(config, issue_id, "impl_result.json") or {}

    if not pr or not issue:
        print("ERROR: Missing pr.json or issue.json", file=sys.stderr)
        sys.exit(1)

    workspace = pr.get("workspace") or config.get("BITBUCKET_WORKSPACE", "")
    repo_name = pr.get("repo") or config.get("BITBUCKET_REPO", "")
    pr_id = pr.get("id") or pr.get("url", "").rstrip("/").split("/")[-1]
    issue_dir = get_issue_dir(config, issue_id)

    comments = list_pr_comments(config, workspace, repo_name, pr_id)
    comments_text = "\n\n".join(
        f"@{c.get('author', {}).get('nickname', 'unknown')}: "
        f"{c.get('content', {}).get('raw', '') or c.get('body', '')}"
        for c in comments
    ) or "(no comments)"

    prompt = LEARN_PROMPT_TEMPLATE.format(
        issue_id=issue_id,
        title=issue["title"],
        impl_summary=json.dumps(impl_result, indent=2),
        comments_text=comments_text,
    )

    result = run_claude(
        prompt,
        working_dir=issue_dir,
        model=config.get("AI_MODEL"),
        max_turns=int(config.get("MAX_TURNS_LEARN", "30")),
        log_file=issue_dir / "logs" / "learn.log",
    )

    lines = result.output.strip().splitlines()
    json_start = next(
        (i for i, l in enumerate(lines) if l.strip().startswith('{"status"')), len(lines)
    )
    learnings_content = "\n".join(lines[:json_start]).strip()

    if learnings_content:
        write_text(config, issue_id, "learnings.md", learnings_content)
        print(f"Learnings written to workspace/{issue_id}/learnings.md")

    print(f"Learn complete: {result.status_json}")
    sys.exit(0)


if __name__ == "__main__":
    main()
