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


def _build_prompt(skill: str, prompt: str, skill_md: str | None,
                  sprint_team: list[str] | None = None,
                  pr_queue_data: dict | None = None) -> str:
    if skill_md:
        skill_section = f"## Skill: {skill}\n\n{skill_md}"
    else:
        skill_section = f"## Skill: {skill}\n\n(No SKILL.md found — apply your best judgment for this skill.)"

    team_section = ""
    if sprint_team:
        members = ", ".join(sprint_team)
        team_section = f"""\

## Sprint Team Context (authoritative — do NOT query for other team members)

The current user's primary sprint board team has exactly these members:
{members}

STRICT: Only include PRs/issues authored by or assigned to these people.
Do NOT include PRs where they are only listed as reviewer.
"""

    data_section = ""
    if pr_queue_data is not None:
        data_section = f"""\

## Pre-Fetched PR Data (USE THIS — do NOT make any API calls)

The following JSON contains ALL sprint team PRs already gathered. Format this data.
DO NOT call any tools to fetch PRs or issues — the data is complete below.

```json
{json.dumps(pr_queue_data, indent=2)}
```
"""

    return f"""\
{skill_section}
{team_section}{data_section}
## Request

{prompt}

After completing, output ONLY this JSON on the last line:
{{"status":"DONE","summary":"<one sentence describing what was done>"}}
Or on failure:
{{"status":"ERROR","reason":"<explanation>"}}
"""


def _resolve_sprint_team(config: dict) -> list[str]:
    """Return the list of Jira displayNames on the current user's primary sprint board.

    Uses the same logic as gather_jira: find sprints containing the current user's
    issues, collect all unique assignees on those sprints.  Falls back to
    STANDUP_TEAM_MEMBERS env var if Jira is not reachable or returns no results.
    """
    try:
        from scripts.standup.gather_jira import (
            _get_my_sprint_ids,
            _fetch_project_boards,
            get_sprint_issues,
        )
        import requests as _req

        jira_url = config.get("JIRA_BASE_URL", "")
        jira_project = config.get("JIRA_PROJECT", "")
        if not jira_url or not jira_project:
            raise ValueError("no jira config")

        my_sprint_ids = _get_my_sprint_ids(config)
        if not my_sprint_ids:
            raise ValueError("no sprint ids found for current user")

        all_boards = _fetch_project_boards(config)
        scrum_boards = [b for b in all_boards if b.get("type", "").lower() == "scrum"]

        # Collect issues from the user's sprints across all boards
        seen_keys: set[str] = set()
        team_names: list[str] = []
        seen_names: set[str] = set()

        for board in scrum_boards:
            board_id = board["id"]
            # Check if any of the user's sprint ids appear in this board's active sprints
            base = jira_url.rstrip("/")
            resp = _req.get(
                f"{base}/rest/agile/1.0/board/{board_id}/sprint",
                params={"state": "active"},
                headers={"Authorization": f"Basic {config.get('JIRA_AUTH', '')}",
                         "Accept": "application/json"},
                timeout=10,
            )
            if not resp.ok:
                continue
            board_sprints = resp.json().get("values", [])
            for sprint in board_sprints:
                if sprint["id"] not in my_sprint_ids:
                    continue
                for issue in get_sprint_issues(config, sprint["id"]):
                    key = issue.get("key", "")
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    assignee = (issue.get("fields") or {}).get("assignee") or {}
                    name = assignee.get("displayName", "")
                    if name and name not in seen_names:
                        seen_names.add(name)
                        team_names.append(name)

        if team_names:
            return team_names
        raise ValueError("no assignees found in sprint")

    except Exception as exc:
        print(f"[adhoc] sprint team resolution failed ({exc}), falling back to STANDUP_TEAM_MEMBERS", flush=True)

    # Fallback: parse STANDUP_TEAM_MEMBERS
    raw = config.get("STANDUP_TEAM_MEMBERS", "").strip()
    if raw in ("<no value>", "{{.StandupTeamMembers}}", ""):
        return []
    return [m.strip() for m in raw.replace(",", "\n").splitlines() if m.strip()]


def _setup_environment(config: dict) -> list[str]:
    """Write .ygs/tracker.yml and set JIRA_AUTH.  Returns resolved sprint team list."""
    import base64
    import os
    import subprocess
    from pathlib import Path as _Path

    workspace = config.get("WORKSPACE_DIR", "/workspace")
    ygs_dir = _Path(workspace) / ".ygs"
    ygs_dir_ok = True
    try:
        ygs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[adhoc] warning: could not create .ygs dir: {e}", file=sys.stderr, flush=True)
        ygs_dir_ok = False

    # Set JIRA_AUTH regardless of whether the .ygs dir exists
    jira_email = config.get("JIRA_EMAIL", "")
    jira_token = config.get("JIRA_API_TOKEN", "")
    if jira_email and jira_token:
        auth = base64.b64encode(f"{jira_email}:{jira_token}".encode()).decode()
        os.environ["JIRA_AUTH"] = auth

    # Resolve sprint team (needs JIRA_AUTH set above)
    sprint_team = _resolve_sprint_team(config)
    if sprint_team:
        team_yaml = "\n".join(f"  - {m}" for m in sprint_team)
        team_block = f"team:\n{team_yaml}\n"
        print(f"[adhoc] sprint team ({len(sprint_team)}): {', '.join(sprint_team)}", flush=True)
    else:
        team_block = ""

    if not ygs_dir_ok:
        # Auth env is set; tracker.yml write skipped
        gh_token = config.get("GH_TOKEN", "")
        if gh_token:
            try:
                subprocess.run(["gh", "auth", "login", "--with-token"],
                               input=gh_token, text=True, capture_output=True)
            except FileNotFoundError:
                pass
        return sprint_team

    tracker = ygs_dir / "tracker.yml"
    jira_url = config.get("JIRA_BASE_URL", "")
    jira_project = config.get("JIRA_PROJECT", "")
    jira_board_id = config.get("JIRA_BOARD_ID", "")
    gh_org = config.get("GH_ORG", "")
    gh_repo = config.get("GH_REPO", "")
    bb_workspace = config.get("BITBUCKET_WORKSPACE", "")
    bb_repo = config.get("BITBUCKET_REPO", "")
    bb_username = config.get("BITBUCKET_USERNAME", "")

    if jira_url and jira_project:
        lines = [
            f"tracker: jira",
            f"project: {jira_project}",
            f"board: {jira_project}",
        ]
        if jira_board_id:
            lines.append(f"board_id: {jira_board_id}")
        lines.append(f"base_url: {jira_url}")
        if bb_workspace and bb_repo:
            lines += [
                f"bitbucket:",
                f"  workspace: {bb_workspace}",
                f"  repo: {bb_repo}",
            ]
            if bb_username:
                lines.append(f"  username: {bb_username}")
        tracker.write_text("\n".join(lines) + "\n" + team_block)
    elif gh_org and gh_repo:
        lines = [
            f"tracker: github",
            f"org: {gh_org}",
            f"repo: {gh_repo}",
        ]
        tracker.write_text("\n".join(lines) + "\n" + team_block)

    gh_token = config.get("GH_TOKEN", "")
    if gh_token:
        try:
            subprocess.run(["gh", "auth", "login", "--with-token"],
                           input=gh_token, text=True, capture_output=True)
        except FileNotFoundError:
            pass

    return sprint_team


@click.command()
@click.option("--skill", required=True, help="Skill name to invoke (e.g. ygs-standup)")
@click.option("--prompt", "prompt_text", required=True, help="Free-form instruction")
def main(skill: str, prompt_text: str) -> None:
    config = load_config()
    validate_claude_config(config)
    sprint_team = _setup_environment(config)

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

    # For ygs-pr-queue: load pre-fetched pr_queue.json and embed in prompt so Claude
    # formats the data rather than making its own API calls.
    pr_queue_data: dict | None = None
    pr_queue_path = workspace / "pr_queue.json"
    if skill == "ygs-pr-queue":
        if pr_queue_path.exists():
            try:
                pr_queue_data = json.loads(pr_queue_path.read_text(encoding="utf-8"))
                print(f"[adhoc] loaded pr_queue.json ({pr_queue_data.get('pr_count', '?')} PRs)", flush=True)
            except Exception as e:
                print(f"[adhoc] warn: could not load pr_queue.json: {e}", flush=True)
        if pr_queue_data is None:
            # Always embed an empty result rather than letting Claude call APIs
            pr_queue_data = {"sprint": "", "pr_count": 0, "prs": []}
            print("[adhoc] pr_queue.json missing — embedding empty result, no API calls", flush=True)

    full_prompt = _build_prompt(skill, prompt_text, skill_md,
                                sprint_team=sprint_team or None,
                                pr_queue_data=pr_queue_data)
    model = config.get("AI_MODEL")
    max_turns = int(config.get("MAX_TURNS_IMPLEMENT", "100"))
    log_path = logs_dir / "adhoc.log"

    # When pr_queue data is embedded, Claude only needs to format text — no tools needed.
    # Passing allowed_tools=None prevents Claude from calling Bash/curl to fetch its own data.
    tools = None if pr_queue_data is not None else "Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS"

    try:
        result = run_claude(
            full_prompt,
            working_dir=workspace,
            model=model,
            max_turns=max_turns,
            log_file=log_path,
            allowed_tools=tools,
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
