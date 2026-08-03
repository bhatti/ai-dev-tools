"""Run a you-got-skills skill with a free-form prompt, post result to Slack thread.

Usage:
    python -m scripts.adhoc.run_skill --skill ygs-standup --prompt "summarize open PRs"

Required env: ANTHROPIC_API_KEY or CLAUDE_CODE_USE_BEDROCK=1
Optional env: SLACK_BOT_TOKEN, SLACK_CHANNEL, SLACK_THREAD_TS

Writes: /workspace/adhoc_result.json
        /workspace/logs/adhoc.log

Exit codes: 0=done, 1=error
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import re

import click
import requests

from scripts.common.claude_runner import run_claude
from scripts.common.config import get_workspace_dir, load_config, validate_claude_config

# Maximum chars of Claude output to post back to Slack
_MAX_SLACK_CHARS = 3000

# Directories to search for skill SKILL.md files (in priority order)
_SKILL_SEARCH_PATHS = [
    Path("/workspace/skills"),
    Path("/workspace/you-got-skills/skills"),
    Path.home() / ".claude" / "skills" / "you-got-skills" / "skills",
    Path.home() / "workplace" / "you-got-skills" / "skills",
]


def _load_skill_md(skill: str) -> str | None:
    """Search known paths for <skill>/SKILL.md and return its content, or None."""
    for base in _SKILL_SEARCH_PATHS:
        candidate = base / skill / "SKILL.md"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return None


def post_to_thread(config: dict, text: str, thread_ts: str | None = None) -> bool:
    """Post a message to the configured Slack channel, optionally in a thread.

    Silently skips when SLACK_BOT_TOKEN or SLACK_CHANNEL is absent.
    Returns True on success, False on any failure.
    """
    token = config.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("[adhoc] SLACK_BOT_TOKEN not set — skipping Slack post", flush=True)
        return False

    channel = config.get("SLACK_CHANNEL", "").lstrip("#")
    if not channel:
        print("[adhoc] SLACK_CHANNEL not set — skipping Slack post", flush=True)
        return False

    payload: dict = {
        "channel": channel,
        "text": text,
        "unfurl_links": False,
        "mrkdwn": True,
    }
    ts = thread_ts or config.get("SLACK_THREAD_TS", "") or None
    if ts:
        payload["thread_ts"] = ts

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    if not resp.ok:
        print(f"[adhoc] Slack HTTP {resp.status_code}", file=sys.stderr, flush=True)
        return False
    data = resp.json()
    if not data.get("ok"):
        print(f"[adhoc] Slack error: {data.get('error', 'unknown')}", file=sys.stderr, flush=True)
        return False
    print(f"[adhoc] result posted to #{channel}", flush=True)
    return True


def _strip_for_slack(text: str) -> str:
    """Remove markdown formatting that Slack renders literally.

    Strips bold/italic markers, inline code, heading prefixes, markdown tables,
    and replaces dash bullets with •. Leaves emoji and plain text intact.
    Skips trailing JSON status line.
    """
    # Remove trailing status JSON line
    lines = text.splitlines()
    if lines and lines[-1].strip().startswith("{") and '"status"' in lines[-1]:
        lines = lines[:-1]
    text = "\n".join(lines).strip()

    # Remove markdown tables (lines that are all pipes/dashes)
    text = re.sub(r"^\|.*\|.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|?[-| :]+\|?\s*$", "", text, flags=re.MULTILINE)

    # Remove bold/italic markers
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)

    # Remove inline code backticks
    text = re.sub(r"`(.+?)`", r"\1", text)

    # Remove markdown heading prefixes
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # Replace markdown dash bullets with •
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.MULTILINE)

    # Remove horizontal rules
    text = re.sub(r"^---+\s*$", "", text, flags=re.MULTILINE)

    # Remove → arrows
    text = re.sub(r"\s*→\s*", ": ", text)

    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _build_prompt(skill: str, prompt: str, skill_md: str | None) -> str:
    if skill_md:
        skill_section = f"## Skill: {skill}\n\n{skill_md}"
    else:
        skill_section = f"## Skill: {skill}\n\n(No SKILL.md found — apply your best judgment for this skill.)"
    return f"""\
{skill_section}

## Request

{prompt}

After completing, output ONLY this JSON on the last line:
{{"status":"DONE","summary":"<one sentence describing what was done>"}}
Or on failure:
{{"status":"ERROR","reason":"<explanation>"}}
"""


def _setup_environment(config: dict) -> None:
    """Write .ygs/tracker.yml and set JIRA_AUTH — replaces shell setup steps in YAML."""
    import base64
    import os
    import subprocess
    from pathlib import Path as _Path

    workspace = config.get("WORKSPACE_DIR", "/workspace")
    ygs_dir = _Path(workspace) / ".ygs"
    try:
        ygs_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    jira_email = config.get("JIRA_EMAIL", "")
    jira_token = config.get("JIRA_API_TOKEN", "")
    if jira_email and jira_token:
        auth = base64.b64encode(f"{jira_email}:{jira_token}".encode()).decode()
        os.environ["JIRA_AUTH"] = auth

    tracker = ygs_dir / "tracker.yml"
    jira_url = config.get("JIRA_BASE_URL", "")
    jira_project = config.get("JIRA_PROJECT", "")
    gh_org = config.get("GH_ORG", "")
    gh_repo = config.get("GH_REPO", "")
    team = config.get("STANDUP_TEAM_MEMBERS", "")
    if jira_url and jira_project:
        tracker.write_text(
            f"tracker: jira\nproject: {jira_project}\nboard: {jira_project}\n"
            f"base_url: {jira_url}\nteam: {team}\n"
        )
    elif gh_org and gh_repo:
        tracker.write_text(
            f"tracker: github\norg: {gh_org}\nrepo: {gh_repo}\nteam: {team}\n"
        )

    gh_token = config.get("GH_TOKEN", "")
    if gh_token:
        try:
            subprocess.run(["gh", "auth", "login", "--with-token"],
                           input=gh_token, text=True, capture_output=True)
        except FileNotFoundError:
            pass


@click.command()
@click.option("--skill", required=True, help="Skill name to invoke (e.g. ygs-standup)")
@click.option("--prompt", "prompt_text", required=True, help="Free-form instruction")
def main(skill: str, prompt_text: str) -> None:
    config = load_config()
    validate_claude_config(config)
    _setup_environment(config)

    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    logs_dir = workspace / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    print(f"[adhoc] skill={skill} prompt={prompt_text[:80]}...", flush=True)

    skill_md = _load_skill_md(skill)
    if skill_md:
        print(f"[adhoc] loaded SKILL.md for {skill} ({len(skill_md)} chars)", flush=True)
    else:
        print(f"[adhoc] no SKILL.md found for {skill} — using fallback prompt", flush=True)

    full_prompt = _build_prompt(skill, prompt_text, skill_md)
    model = config.get("AI_MODEL")
    max_turns = int(config.get("MAX_TURNS_IMPLEMENT", "100"))
    log_path = logs_dir / "adhoc.log"

    try:
        result = run_claude(
            full_prompt,
            working_dir=workspace,
            model=model,
            max_turns=max_turns,
            log_file=log_path,
        )
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr, flush=True)
        _write_result(workspace, {"status": "ERROR", "reason": str(e)})
        # Post error to thread
        post_to_thread(config, f"⚠️ Skill `/{skill}` failed: {str(e)[:500]}")
        sys.exit(1)

    status_data = result.status_json or {"status": result.status}
    _write_result(workspace, status_data)

    # Post a meaningful slice of the output back to Slack
    output_to_post = _strip_for_slack(result.output.strip())
    if len(output_to_post) > _MAX_SLACK_CHARS:
        # Prefer the end of the output (likely to have the summary)
        output_to_post = "…\n" + output_to_post[-_MAX_SLACK_CHARS:]

    if output_to_post:
        post_to_thread(config, output_to_post)
    else:
        summary = status_data.get("summary", "")
        post_to_thread(config, f"✅ `/{skill}` complete. {summary}")

    print(f"[adhoc] status={status_data.get('status')}", flush=True)

    if status_data.get("status") in ("DONE", "DONE_WITH_CONCERNS", "MAX_TURNS_REACHED"):
        sys.exit(0)
    sys.exit(1)


def _write_result(workspace: Path, data: dict) -> None:
    p = workspace / "adhoc_result.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
