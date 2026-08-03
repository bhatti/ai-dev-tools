"""Run a full PR review using the ygs-review-pr skill via Claude Code.

Usage:
    python -m scripts.review.run --pr-url <url> [--skill ygs-review-pr] [--issue-id <optional>]

Required env: ANTHROPIC_API_KEY or CLAUDE_CODE_USE_BEDROCK=1
Optional env: GH_TOKEN, BITBUCKET_TOKEN, BITBUCKET_USERNAME, GH_ORG, GH_REPO

Reads:  env vars
Writes: /workspace/review_result.json
        /workspace/findings.json
        /workspace/logs/review.log
        /workspace/logs/review.prompt.txt

Exit codes: 0=done, 1=error
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from scripts.common.claude_runner import run_claude
from scripts.common.config import get_workspace_dir, load_config, validate_claude_config


REVIEW_PROMPT_TEMPLATE = """\
You are an expert code reviewer. Your task is to review a pull request.

## PR URL

{pr_url}

## Instructions

1. Invoke the `/{skill}` skill to perform a full PR review of the PR at the URL above.
   Pass the PR URL as context so the skill knows which PR to fetch and review.

2. After the skill completes, write your findings to `findings.json` in the current
   working directory using this exact structure:

```json
{{
  "pr_url": "{pr_url}",
  "verdict": "APPROVE | REQUEST_CHANGES | COMMENT",
  "findings": [
    {{
      "severity": "CRITICAL | HIGH | MEDIUM | LOW",
      "confidence": "HIGH | MEDIUM | LOW",
      "title": "<short one-line title>",
      "file": "<path/to/file or empty string>",
      "line": null,
      "domain": "correctness | security | api | sre",
      "description": "<what is wrong and why it matters>",
      "fix": "<concrete suggested fix>"
    }}
  ],
  "summary": "<one sentence overall assessment>"
}}
```

3. If `findings.json` already exists from the skill run, do not overwrite it — leave it as-is.

4. Output ONLY this JSON on the last line (no text after it):
   {{"status":"DONE","findings_count":<N>,"verdict":"<APPROVE|REQUEST_CHANGES|COMMENT>","summary":"<one sentence>"}}

   Or if something went wrong:
   {{"status":"ERROR","reason":"<explanation>"}}
"""


@click.command()
@click.option("--pr-url", required=True, help="Full URL or number of the PR to review")
@click.option("--skill", default="ygs-review-pr", show_default=True, help="Skill name to invoke")
@click.option("--issue-id", default=None, help="Optional issue ID for namespacing (unused, kept for compat)")
def main(pr_url: str, skill: str, issue_id: str | None) -> None:
    config = load_config()
    validate_claude_config(config)

    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    logs_dir = workspace / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    print(f"[review] pr_url={pr_url} skill={skill}", flush=True)

    # Build prompt
    prompt = REVIEW_PROMPT_TEMPLATE.format(pr_url=pr_url, skill=skill)

    # Save prompt for debugging
    prompt_path = logs_dir / "review.prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    model = config.get("AI_MODEL")
    max_turns = int(config.get("MAX_TURNS_IMPLEMENT", "100"))
    log_path = logs_dir / "review.log"

    print(f"[review] Running review with model={model}, max_turns={max_turns}", flush=True)
    try:
        result = run_claude(
            prompt,
            working_dir=workspace,
            model=model,
            max_turns=max_turns,
            log_file=log_path,
        )
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr, flush=True)
        _write_json(workspace / "review_result.json", {"status": "ERROR", "reason": str(e)})
        _ensure_findings_stub(workspace / "findings.json", pr_url)
        sys.exit(1)

    # Parse status JSON from result
    status_data: dict = result.status_json or {"status": result.status}
    _write_json(workspace / "review_result.json", status_data)

    # Ensure findings.json exists (Claude should have written it; stub if not)
    findings_path = workspace / "findings.json"
    if not findings_path.exists():
        verdict = status_data.get("verdict", "COMMENT")
        summary = status_data.get("summary", "Review completed.")
        _write_json(findings_path, {
            "pr_url": pr_url,
            "verdict": verdict,
            "findings": [],
            "summary": summary,
        })

    findings_count = status_data.get("findings_count", 0)
    verdict = status_data.get("verdict", "COMMENT")
    summary = status_data.get("summary", "")

    print(f"[review] status={status_data.get('status')} findings={findings_count} verdict={verdict}", flush=True)
    print(f"[review] summary: {summary}", flush=True)

    if status_data.get("status") in ("DONE", "DONE_WITH_CONCERNS"):
        sys.exit(0)

    # Any other status (ERROR, BLOCKED) is a failure
    print(f"ERROR: unexpected review status '{status_data.get('status')}'", file=sys.stderr, flush=True)
    sys.exit(1)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _ensure_findings_stub(path: Path, pr_url: str) -> None:
    if not path.exists():
        _write_json(path, {
            "pr_url": pr_url,
            "verdict": "COMMENT",
            "findings": [],
            "summary": "Review did not complete — see review.log for details.",
        })


if __name__ == "__main__":
    main()
