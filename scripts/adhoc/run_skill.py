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
import os
import sys
from pathlib import Path

import re

import click
import requests

from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS, _ensure_ygs_skills, _KNOWN_SKILLS
from scripts.common.config import get_workspace_dir, load_config, validate_claude_config, MODEL_SHORTNAMES
from scripts.standup.slack_client import build_mrkdwn_blocks, build_pr_blocks, notify as slack_notify

# Maximum chars of Claude output to post back to Slack
_MAX_SLACK_CHARS = 3000


def _skill_search_paths() -> list[Path]:
    """Return skill search paths in priority order (highest priority first).

    Resolution order:
    1. Codebase-local skills: CODEBASE_DIR/.claude/skills/<name>/SKILL.md
       (applied as symlink overrides by entrypoint.sh; also checked here for
       direct Python-side SKILL.md loading)
    2. Installed skills: ~/.claude/skills/<name>/SKILL.md
       (ygs base + project overrides, symlinked by entrypoint.sh on startup)
    3. Local dev fallback: ~/.claude/skills/you-got-skills/skills
       (the you-got-skills repo itself, symlinked there by entrypoint.sh or
        the you-got-skills/setup script when running outside a container)
    """
    paths: list[Path] = []
    codebase_dir = os.environ.get("CODEBASE_DIR", "").strip()
    if codebase_dir:
        paths.append(Path(codebase_dir) / ".claude" / "skills")
    paths.append(Path.home() / ".claude" / "skills")
    paths.append(Path.home() / ".claude" / "skills" / "you-got-skills" / "skills")
    return paths


def _load_skill_md(skill: str) -> str | None:
    """Search known paths for <skill>/SKILL.md and return its content, or None."""
    for base in _skill_search_paths():
        candidate = base / skill / "SKILL.md"
        if candidate.exists():
            print(f"[adhoc] skill path: {candidate}", flush=True)
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


def _table_row_to_bullet(line: str) -> str:
    """Convert a markdown table data row '| col1 | col2 |' to '• col1 · col2'."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    cells = [c for c in cells if c]
    return "• " + " · ".join(cells) if cells else ""


def _strip_for_slack(text: str) -> str:
    """Remove markdown formatting that Slack renders literally.

    Converts markdown table rows to • bullet rows (col1 · col2).
    Preserves • bullets already in the correct format.
    Skips trailing JSON status line.
    """
    # Remove trailing status JSON line
    lines = text.splitlines()
    if lines and lines[-1].strip().startswith("{") and '"status"' in lines[-1]:
        lines = lines[:-1]

    # Convert markdown tables: data rows → bullets, separator rows → blank
    converted: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^\|", stripped):
            # Separator row (e.g. |---|---|) — drop it
            if re.match(r"^\|[\s|:-]+\|$", stripped):
                converted.append("")
            else:
                converted.append(_table_row_to_bullet(stripped))
        else:
            converted.append(line)
    text = "\n".join(converted).strip()

    # Remove bold/italic markers
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)

    # Remove inline code backticks
    text = re.sub(r"`(.+?)`", r"\1", text)

    # Remove markdown heading prefixes (ALL-CAPS section headers are already correct)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # Convert '- item' / '* item' dash bullets to • — preserve lines already starting with •
    text = re.sub(r"^[ \t]*[-*][ \t]+", "• ", text, flags=re.MULTILINE)

    # Remove horizontal rules
    text = re.sub(r"^---+\s*$", "", text, flags=re.MULTILINE)

    # Remove → arrows
    text = re.sub(r"\s*→\s*", ": ", text)

    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


_TRACKER_SKILLS = {"ygs-risk-scan", "ygs-standup", "ygs-pr-queue"}


# Built-in fallback descriptions for skills that may not have a SKILL.md installed.
# These short descriptions steer Claude when no detailed skill file is found.
_SKILL_FALLBACK_DESCRIPTIONS: dict[str, str] = {
    "ygs-ask": (
        "Answer the user's question directly using your knowledge and, when relevant, "
        "Jira/GitHub API calls via Bash to fetch project-specific data. "
        "You do NOT need a local codebase or any source files — answer from knowledge. "
        "If the question is general (not project-specific), respond immediately from knowledge. "
        "Keep the answer concise and Slack-formatted."
    ),
    "ygs-pr-comments": (
        "Fetch and display all existing review comments on the PR whose URL is in $SKILL_PROMPT. "
        "Do NOT run an AI code review — only show existing human comments.\n\n"
        "1. Parse the URL to detect platform and extract org/repo/pr-number.\n"
        "2. For github.com URLs, use the gh CLI:\n"
        "   PR_URL=\"$SKILL_PROMPT\"\n"
        "   gh pr view \"$PR_URL\" --json title,state,author,body\n"
        "   gh pr view \"$PR_URL\" --json reviews --jq '.reviews[] | "
        "{author:.author.login, state:.state, body:.body}'\n"
        "   gh pr view \"$PR_URL\" --json reviewThreads --jq "
        "'.reviewThreads[] | .comments[] | {author:.author.login, path:.path, line:.line, body:.body}'\n"
        "3. For bitbucket.org URLs, extract workspace/repo/pr-id from the URL and call:\n"
        "   curl -s -u \"$BITBUCKET_USERNAME:$BITBUCKET_TOKEN\" \\\n"
        "     \"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}/pullrequests/{id}/comments\"\n"
        "4. Format for Slack: PR title + state at top, then each comment with "
        "author, file:line (for inline), and body. Skip bot/automated comments. "
        "If there are no comments say so clearly."
    ),
}


def _build_prompt(skill: str, prompt: str, skill_md: str | None,
                  sprint_team: list[str] | None = None) -> str:
    if skill_md:
        skill_section = f"## Skill: {skill}\n\n{skill_md}"
    elif skill in _SKILL_FALLBACK_DESCRIPTIONS:
        skill_section = f"## Skill: {skill}\n\n{_SKILL_FALLBACK_DESCRIPTIONS[skill]}"
    else:
        skill_section = f"## Skill: {skill}\n\n(No SKILL.md found — apply your best judgment for this skill.)"

    # Tracker skills run in a bare workspace with no git repo.
    # Claude must use Jira/Bitbucket/GitHub APIs for all data — never git commands.
    env_context = ""
    if skill in _TRACKER_SKILLS:
        env_context = """\

## Execution Environment

IMPORTANT: You are running in a bare workspace directory. There is NO git repository here.
Do NOT run git commands. Do NOT look for source code.
All data MUST come from the Jira/Bitbucket/GitHub APIs using the credentials available
in environment variables (JIRA_AUTH, JIRA_BASE_URL, GH_TOKEN, etc.).
The `.ygs/tracker.yml` file in the current directory has the project/board configuration.
"""

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

    return f"""\
{skill_section}
{env_context}{team_section}
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
            get_active_sprints,
            get_sprint_issues,
        )

        jira_url = config.get("JIRA_BASE_URL", "")
        jira_project = config.get("JIRA_PROJECT", "")
        if not jira_url or not jira_project:
            raise ValueError("no jira config")

        active_sprints = get_active_sprints(config)
        if not active_sprints:
            raise ValueError("no active sprints found")

        seen_keys: set[str] = set()
        team_names: list[str] = []
        seen_names: set[str] = set()

        for sprint in active_sprints:
            sprint_id = sprint.get("id")
            if not sprint_id:
                continue
            for issue in get_sprint_issues(config, sprint_id):
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


_SKILL_SYSTEM_PROMPT_MAP: dict[str, str] = {
    "ygs-standup": "standup",
    "ygs-risk-scan": "standup",
    "ygs-pr-queue": "standup",
    "ygs-review-pr": "review",
    "ygs-review-deep": "review",
    "ygs-code-review": "review",
    "ygs-security-review": "review",
    "ygs-sre-review": "review",
    "ygs-api-review": "review",
    "ygs-ui-review": "review",
    "ygs-implement": "implement",
    "ygs-ship": "implement",
    "ygs-learn": "learn",
    "ygs-retro": "learn",
    "ygs-ask": "adhoc",
}


_REVIEW_URL_PATTERN = re.compile(
    r'https?://\S+(?:pull-request|pull/|/pr/|/merge_request)\S*', re.IGNORECASE
)

# Ordered preference list for review skill selection — first installed one wins.
# Not hardcoded behavior: if none are installed, routing falls back to the original skill.
_REVIEW_SKILL_PREFERENCE = ["ygs-review-deep", "ygs-review-pr"]


def _best_review_skill(deep: bool) -> str | None:
    """Return the best installed review skill, preferring deep variants when requested.

    Checks the curated preference list first (no need for double-filtering since the
    list only contains review skills), then falls back to any installed review-category
    skill from _SKILL_SYSTEM_PROMPT_MAP.
    """
    candidates = _REVIEW_SKILL_PREFERENCE if deep else _REVIEW_SKILL_PREFERENCE[1:]
    for candidate in candidates:
        if candidate in _KNOWN_SKILLS:
            return candidate
    # Fall back to any installed review-category skill
    review_skills = {s for s, cat in _SKILL_SYSTEM_PROMPT_MAP.items() if cat == "review"}
    available = sorted(review_skills & _KNOWN_SKILLS)
    return available[0] if available else None


def _detect_intent(prompt: str, skill: str) -> str:
    """Override skill based on prompt content when routed generically via ygs-ask.

    Detects PR review URLs and routes to the best available review skill so that
    messages like "deep review <URL>" use ygs-review-deep if installed, else the
    best available review skill. Falls back to original skill if none are installed.
    """
    if skill != 'ygs-ask':
        return skill
    if _REVIEW_URL_PATTERN.search(prompt):
        deep = 'deep' in prompt.lower()
        target = _best_review_skill(deep)
        return target if target else skill
    return skill


def _system_prompt_for_skill(skill: str) -> str:
    """Return the appropriate system prompt key for a given skill name."""
    key = _SKILL_SYSTEM_PROMPT_MAP.get(skill, "adhoc")
    return SYSTEM_PROMPTS[key]


@click.command()
@click.option("--skill", required=True, help="Skill name to invoke (e.g. ygs-standup)")
@click.option("--prompt", "prompt_text", required=True, help="Free-form instruction")
def main(skill: str, prompt_text: str) -> None:
    config = load_config()
    # NOTE: validate_claude_config is called AFTER the ygs-pr-queue early-exit branch
    # so skills that never invoke Claude don't fail on missing credentials.
    sprint_team = _setup_environment(config)

    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    logs_dir = workspace / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Override skill based on prompt intent before loading skill metadata.
    skill = _detect_intent(prompt_text, skill)

    print(f"[adhoc] skill={skill} prompt={prompt_text[:80]}...", flush=True)

    # Ensure YGS skills are installed before attempting to load SKILL.md
    _ensure_ygs_skills()
    skill_md = _load_skill_md(skill)
    if skill_md:
        print(f"[adhoc] loaded SKILL.md for {skill} ({len(skill_md)} chars)", flush=True)
    else:
        print(f"[adhoc] no SKILL.md found for {skill} — using fallback prompt", flush=True)

    # Emit task context early — before any sys.exit() so all paths are covered.
    _tracker_val = config.get("DEFAULT_TRACKER") or config.get("SELECTED_TRACKER") or ""
    _model_val = config.get("AI_MODEL") or ""
    print(f"::add-task-context SELECTED_TRACKER::{_tracker_val}", flush=True)
    print(f"::add-task-context SKILL::{skill}", flush=True)
    print(f"::add-task-context SKILL_LOADED::{'yes' if skill_md else 'no'}", flush=True)
    print(f"::add-task-context SELECTED_MODEL::{_model_val}", flush=True)

    # For ygs-pr-queue: load pre-fetched pr_queue.json and build Block Kit directly —
    # no Claude invocation needed; the data is already structured.
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
            pr_queue_data = {"sprint": "", "pr_count": 0, "prs": []}
            print("[adhoc] pr_queue.json missing — using empty result", flush=True)

        # Build and post Block Kit directly — skip Claude for pr-queue
        from datetime import date as _date
        sprint = pr_queue_data.get("sprint", "")
        title = f"PR Queue — {sprint} — {_date.today().isoformat()}" if sprint else f"PR Queue — {_date.today().isoformat()}"
        blocks = build_pr_blocks(title, pr_queue_data)
        pr_count = pr_queue_data.get("pr_count", 0)
        fallback_text = f"PR Queue — {pr_count} open PR(s) in {sprint}" if sprint else f"PR Queue — {pr_count} open PR(s)"
        try:
            slack_notify(config, fallback_text, blocks=blocks)
        except Exception as _se:
            print(f"[adhoc] WARNING: Slack post failed (non-fatal): {_se}", flush=True)

        status_data = {
            "status": "DONE",
            "summary": f"{pr_count} sprint PR(s): {fallback_text}",
        }
        _write_result(workspace, status_data)
        print(f"[adhoc] pr-queue posted {pr_count} PRs to Slack", flush=True)
        sys.exit(0)

    # Validate Claude credentials only for skills that actually call Claude.
    # ygs-pr-queue exits above without ever invoking the Claude CLI.
    validate_claude_config(config)

    full_prompt = _build_prompt(skill, prompt_text, skill_md,
                                sprint_team=sprint_team or None)

    # Resolve model: AI_MODEL_OVERRIDE (from router model-override parsing) >
    # AI_MODEL config (from Formicary job variable) > default (claude picks).
    model = config.get("AI_MODEL")
    model_override = os.getenv("AI_MODEL_OVERRIDE", "").strip()
    if model_override and model_override not in ("<no value>", "{{.AiModel}}"):
        # Resolve short names ("haiku", "sonnet", "opus", "sonnet-5", …) to full model IDs.
        # config values (from env/job params) take precedence over the module constants.
        _shortnames = {
            "haiku":  config.get("ANTHROPIC_DEFAULT_HAIKU_MODEL",  MODEL_SHORTNAMES["haiku"]),
            "sonnet": config.get("ANTHROPIC_DEFAULT_SONNET_MODEL", MODEL_SHORTNAMES["sonnet"]),
            "opus":   config.get("ANTHROPIC_DEFAULT_OPUS_MODEL",   MODEL_SHORTNAMES["opus"]),
            **{k: v for k, v in MODEL_SHORTNAMES.items() if k not in ("haiku", "sonnet", "opus")},
        }
        model = _shortnames.get(model_override.lower(), model_override)

    max_turns = int(config.get("MAX_TURNS_ADHOC", config.get("MAX_TURNS_IMPLEMENT", "50")))
    log_path = logs_dir / "adhoc.log"

    try:
        result = run_claude(
            full_prompt,
            working_dir=workspace,
            model=model,
            max_turns=max_turns,
            log_file=log_path,
            allowed_tools="Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS,Skill",
            system_prompt=_system_prompt_for_skill(skill),
            primary_skill=skill,
        )
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr, flush=True)
        _write_result(workspace, {"status": "ERROR", "reason": str(e)})
        try:
            slack_notify(config, f"⚠️ Skill `/{skill}` failed: {str(e)[:500]}")
        except Exception as _se:
            print(f"[adhoc] WARNING: Slack error notification failed (non-fatal): {_se}", flush=True)
        sys.exit(1)

    status_data = result.status_json or {"status": result.status}
    _write_result(workspace, status_data)

    # For tracker skills, check if Claude wrote a report file (risk_report.md, etc.)
    # and use it as the output if result.output is only the JSON status line.
    output_text = result.output.strip()
    report_candidates = ["standup_brief.md", "risk_report.md", "adhoc_report.md", "pr_queue_report.md"]
    for candidate in report_candidates:
        report_path = workspace / candidate
        if report_path.exists():
            file_content = report_path.read_text(encoding="utf-8").strip()
            if file_content:
                output_text = file_content
                print(f"[adhoc] using report from {candidate} ({len(file_content)} chars)", flush=True)
                # Generate HTML companion if not already present
                html_path = workspace / candidate.replace(".md", ".html")
                if not html_path.exists():
                    try:
                        from scripts.common.report_renderer import render_simple_html
                        html_path.write_text(render_simple_html(skill, file_content), encoding="utf-8")
                        print(f"[adhoc] wrote {html_path.name}", flush=True)
                    except Exception as _he:
                        print(f"[adhoc] WARNING: could not write HTML: {_he}", flush=True)
                # Also write standardized reports/ directory
                try:
                    from scripts.common.report_renderer import render_simple_html
                    reports_dir = workspace / "reports"
                    reports_dir.mkdir(parents=True, exist_ok=True)
                    (reports_dir / "report.md").write_text(file_content, encoding="utf-8")
                    (reports_dir / "report.html").write_text(render_simple_html(skill, file_content), encoding="utf-8")
                    (reports_dir / "result.json").write_text(json.dumps(status_data, indent=2), encoding="utf-8")
                    print(f"[adhoc] wrote reports/report.md, reports/report.html, reports/result.json", flush=True)
                except Exception as _re:
                    print(f"[adhoc] WARNING: could not write reports/: {_re}", flush=True)
                break

    # If no report candidate found but Claude produced output, still write reports/
    else:
        if output_text and output_text != json.dumps(status_data):
            try:
                from scripts.common.report_renderer import render_simple_html
                reports_dir = workspace / "reports"
                reports_dir.mkdir(parents=True, exist_ok=True)
                (reports_dir / "report.md").write_text(output_text, encoding="utf-8")
                (reports_dir / "report.html").write_text(render_simple_html(skill, output_text), encoding="utf-8")
                (reports_dir / "result.json").write_text(json.dumps(status_data, indent=2), encoding="utf-8")
                print(f"[adhoc] wrote reports/ from Claude output", flush=True)
            except Exception as _re:
                print(f"[adhoc] WARNING: could not write reports/: {_re}", flush=True)

    output_to_post = _strip_for_slack(output_text)
    if len(output_to_post) > _MAX_SLACK_CHARS:
        output_to_post = output_to_post[:_MAX_SLACK_CHARS] + "\n…\n_(Full report in Formicary job artifacts)_"

    blocks = build_mrkdwn_blocks(output_to_post) if output_to_post else None

    try:
        if output_to_post or blocks:
            slack_notify(config, output_to_post or f"✅ `/{skill}` complete.", blocks=blocks)
        else:
            summary = status_data.get("summary", "")
            slack_notify(config, f"✅ `/{skill}` complete. {summary}")
    except Exception as _se:
        print(f"[adhoc] WARNING: Slack post failed (non-fatal): {_se}", flush=True)

    print(f"[adhoc] status={status_data.get('status')}", flush=True)
    # Re-emit with final resolved model (may differ from config if AI_MODEL_OVERRIDE was used).
    print(f"::add-task-context SELECTED_MODEL::{model or ''}", flush=True)

    # If we reach here, Claude itself exited 0 (non-zero Claude exit raises RuntimeError
    # and calls sys.exit(1) above). Exit 0 so Formicary collects artifacts.
    sys.exit(0)


def _write_result(workspace: Path, data: dict) -> None:
    p = workspace / "adhoc_result.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
