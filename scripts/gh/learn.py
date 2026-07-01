"""Extract learnings from a completed PR lifecycle.

Usage:
    python -m scripts.gh.learn --issue-id 42

Required env: GH_ORG, GH_REPO, GH_TOKEN
Reads:  /workspace/{issue_id}/pr.json
        /workspace/{issue_id}/impl_result.json
Writes: /workspace/{issue_id}/learnings.md

Exit codes: 0=done, 1=error
"""

import json
import sys

import click

from scripts.common.artifacts import read_json, read_text, write_json, write_text
from scripts.common.claude_runner import run_claude
from scripts.common.config import get_issue_dir, load_config, validate_claude_config
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


LEARN_PROMPT_TEMPLATE = """\
You are an expert software engineer extracting learnings from a completed AI coding session.

## Issue #{issue_id}: {title}

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

    comments = fetch_pr_comments(org, repo, pr_number)
    comments_text = "\n\n".join(
        f"@{c.get('user', {}).get('login', 'unknown')}: {c.get('body', '')}"
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

    # Extract any markdown content from output (before the final JSON)
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
