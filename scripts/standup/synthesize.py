"""Synthesize standup brief from gathered signals using the /ygs-standup skill.

Usage:
    python -m scripts.standup.synthesize

Required env: JIRA_BASE_URL+JIRA_EMAIL+JIRA_API_TOKEN (Jira tracker)
           OR GH_ORG+GH_REPO+GH_TOKEN (GitHub tracker)
           Plus Claude API access (ANTHROPIC_API_KEY or CLAUDE_CODE_USE_BEDROCK=1)

Reads:  /workspace/signals.json
Writes: /workspace/standup_brief.md
        /workspace/risk_report.md
        /workspace/synthesize_result.json

Exit codes: 0=done, 1=error
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS, _ensure_ygs_skills
from scripts.common.config import load_config, get_workspace_dir, validate_claude_config


# ---------------------------------------------------------------------------
# Skill loading — read SKILL.md directly (same pattern as adhoc/run_skill.py)
# ---------------------------------------------------------------------------

def _skill_search_paths() -> list[Path]:
    paths: list[Path] = []
    codebase_dir = os.environ.get("CODEBASE_DIR", "").strip()
    if codebase_dir:
        paths.append(Path(codebase_dir) / ".claude" / "skills")
    paths.append(Path.home() / ".claude" / "skills")
    paths.append(Path.home() / ".claude" / "skills" / "you-got-skills" / "skills")
    return paths


def _load_skill_md(skill: str) -> str | None:
    for base in _skill_search_paths():
        candidate = base / skill / "SKILL.md"
        if candidate.exists():
            print(f"[synthesize] skill path: {candidate}", flush=True)
            return candidate.read_text(encoding="utf-8")
    return None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYNTHESIZE_PROMPT = """\
Generate today's standup brief using the instructions below.

The signals have already been gathered and saved to signals.json in the working directory.
Do NOT re-fetch data from Jira, GitHub, or Slack — use only what is in signals.json.

TODAY: {today}

SIGNALS FILE: signals.json (already written to the working directory)

TEAM: {team_members_note}

BOARDS: {boards_json}

STRICT DATA RULES:
• Use ONLY data from signals.json — never fetch or invent additional issues or PRs.
• Report on all sprint assignees — do NOT add notes about team roster or configuration files.
• Only show PRs from open_prs[] in signals.json. Never add aged/org-wide/unrelated PRs.

## Standup Skill Instructions

{skill_instructions}

After completing, write:
• standup_brief.md  — the STANDUP_BRIEF section content
• risk_report.md    — the RISK_REPORT section content

Output ONLY this JSON on the last line:
{{"status":"DONE","risk_count":<N>,"discussion_questions":<N>,"silence_count":<N>}}
"""

_SYNTHESIZE_PROMPT_FALLBACK = """\
Generate today's standup brief from signals.json in the working directory.

The signals have already been gathered. Do NOT re-fetch data from Jira, GitHub, or Slack.

TODAY: {today}

TEAM: {team_members_note}

BOARDS: {boards_json}

STRICT DATA RULES:
• Use ONLY data from signals.json — never fetch or invent additional issues or PRs.
• Report on all sprint assignees — do NOT add notes about team roster or configuration files.
• Only show PRs from open_prs[] in signals.json. Never add aged/org-wide/unrelated PRs.

## Instructions

1. Read signals.json from the current working directory.
2. For each team member in TEAM ROSTER, summarise:
   - Issues they are working on (status, blockers)
   - Open PRs awaiting review
   - Any stale items (not updated in 2+ days)
3. Identify risks: sprint burn-down, blocked issues, PRs with no reviewer.
4. Format output in two sections:

#### STANDUP_BRIEF
Slack-safe mrkdwn. One bullet per person. Keep it to 3-5 lines total.
Format: *Name* — <what they're working on> [BLOCKED: reason if blocked]

#### RISK_REPORT
Bullet list of risks ranked by severity. One line each.

Write standup_brief.md with the STANDUP_BRIEF content and risk_report.md with the RISK_REPORT content.

Output ONLY this JSON on the last line:
{{"status":"DONE","risk_count":<N>,"discussion_questions":<N>,"silence_count":<N>}}
"""


def _build_prompt(signals: dict) -> str:
    team_members = signals.get("team_members", [])

    # Build boards summary: board_id → {board_name, sprint_name, end_date}
    boards_map: dict[str, dict] = {}
    for s in signals.get("all_sprints", []):
        bid = s.get("board_id")
        if bid is not None:
            boards_map[str(bid)] = {
                "board_name": s.get("board", ""),
                "sprint_name": s.get("name", ""),
                "end_date": s.get("end_date", ""),
            }
    for k, v in signals.get("board_sprint_map", {}).items():
        if k not in boards_map:
            boards_map[k] = {
                "board_name": v.get("board", ""),
                "sprint_name": v.get("name", ""),
                "end_date": v.get("end_date", ""),
            }

    # When no explicit team list is configured, report on all sprint assignees.
    # Avoid passing empty [] which causes the model to add notes about team config.
    if team_members:
        team_members_note = ", ".join(team_members)
    else:
        team_members_note = "all sprint assignees (no filter configured)"

    common = dict(
        today=date.today().isoformat(),
        team_members_note=team_members_note,
        boards_json=json.dumps(boards_map, indent=2),
    )

    skill_md = _load_skill_md("ygs-standup")
    if skill_md:
        print(f"[synthesize] loaded ygs-standup SKILL.md ({len(skill_md)} chars)", flush=True)
        return _SYNTHESIZE_PROMPT.format(skill_instructions=skill_md, **common)

    print("[synthesize] WARNING: ygs-standup/SKILL.md not found — using fallback instructions", flush=True)
    return _SYNTHESIZE_PROMPT_FALLBACK.format(**common)


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def _truncate_brief(text: str) -> str:
    """Remove any risk report content that leaked into the brief."""
    stop_patterns = [
        r"^#{1,4}\s+RISK",        # ## RISK REPORT heading
        r"^RISK REPORT",           # plain heading
        r"^---\s*$",               # horizontal rule separator
        r"^```json",               # status JSON fence
        r"^\s*\{\"status\"",       # bare JSON status line {"status":"DONE",...}
    ]
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for pat in stop_patterns:
            if re.match(pat, line.strip(), re.IGNORECASE):
                return "\n".join(lines[:i]).strip()
    return text


def _strip_markdown(text: str) -> str:
    """Normalise markdown to Slack mrkdwn.

    Keeps *single-star bold* (valid Slack mrkdwn for section headers).
    Strips **double-star** markdown bold (not valid in Slack), __, backtick code.
    Converts '- item' dash bullets to '• item'.
    """
    # Strip markdown **double-star** bold (not Slack mrkdwn)
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    # Strip __double-underscore__ bold
    text = re.sub(r'__(.+?)__', r'\1', text)
    # Remove inline code backticks
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Remove markdown heading prefixes (#, ##, etc.)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Convert '- item' dash bullets to • (but not lines starting with * which may be *Bold*)
    text = re.sub(r'^[ \t]-[ \t]+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^-[ \t]+', '• ', text, flags=re.MULTILINE)
    # Remove arrows that sneak in
    text = re.sub(r'\s*→\s*', ': ', text)
    return text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config(required=[])
    validate_claude_config(config)
    workspace_dir = get_workspace_dir(config)

    signals_path = workspace_dir / "signals.json"
    if not signals_path.exists():
        print("ERROR: /workspace/signals.json not found — run gather step first", file=sys.stderr)
        sys.exit(1)

    signals = json.loads(signals_path.read_text())
    tracker = signals.get("tracker", "unknown")
    print(f"[synthesize] tracker={tracker} issues={len(signals.get('issues', []))} prs={len(signals.get('open_prs', []))}", flush=True)

    # Ensure YGS skills are cloned before _build_prompt() tries to load SKILL.md
    _ensure_ygs_skills()
    prompt = _build_prompt(signals)

    try:
        result = run_claude(
            prompt,
            working_dir=workspace_dir,
            model=config.get("AI_MODEL"),
            max_turns=int(config.get("MAX_TURNS_STANDUP", "30")),
            log_file=workspace_dir / "logs" / "synthesize.log",
            allowed_tools="Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS",
            system_prompt=SYSTEM_PROMPTS["standup"],
        )
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        (workspace_dir / "synthesize_result.json").write_text(
            json.dumps({"status": "ERROR", "reason": str(e)})
        )
        sys.exit(1)

    brief_path = workspace_dir / "standup_brief.md"
    risk_path = workspace_dir / "risk_report.md"

    # The skill instructs Claude to write standup_brief.md and risk_report.md directly.
    # Read those files as the source of truth; never parse Claude's conversational output.
    if brief_path.exists():
        brief = _strip_markdown(_truncate_brief(brief_path.read_text().strip()))
    else:
        print("WARNING: standup_brief.md not written by Claude — standup may be incomplete", flush=True)
        brief = ""

    risk_report = risk_path.read_text().strip() if risk_path.exists() else ""

    # Normalise and write back
    brief_path.write_text(brief)
    if risk_report:
        risk_path.write_text(risk_report)

    status_data = result.status_json or {"status": result.status}
    (workspace_dir / "synthesize_result.json").write_text(json.dumps(status_data, indent=2))

    # Write standardized reports/ directory
    try:
        from scripts.common.report_renderer import render_simple_html
        reports_dir = workspace_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "report.md").write_text(brief)
        (reports_dir / "report.html").write_text(render_simple_html("Standup Brief", brief))
        (reports_dir / "result.json").write_text(json.dumps(status_data, indent=2))
        print(f"[synthesize] wrote reports/report.md, reports/report.html, reports/result.json", flush=True)
    except Exception as _re:
        print(f"[synthesize] WARNING: could not write reports/: {_re}", flush=True)

    sj = result.status_json or {}
    if result.status == "DONE":
        print(
            f"[synthesize] brief written — risks={sj.get('risk_count', '?')} "
            f"questions={sj.get('discussion_questions', '?')}",
            flush=True,
        )
    else:
        print(f"[synthesize] unexpected status '{result.status}'", flush=True)

    model = config.get("AI_MODEL", "")
    print(f"::add-task-context SELECTED_MODEL::{model}")
    print(f"::add-task-context SELECTED_TRACKER::{tracker}")
    print(f"::add-task-context ISSUE_COUNT::{len(signals.get('issues', []))}")
    print(f"::add-task-context PR_COUNT::{len(signals.get('open_prs', []))}")
    if sj.get("risk_count") is not None:
        print(f"::add-task-context RISK_COUNT::{sj.get('risk_count', 0)}")
    if sj.get("silence_count") is not None:
        print(f"::add-task-context SILENCE_COUNT::{sj.get('silence_count', 0)}")
    sys.exit(0)


if __name__ == "__main__":
    from scripts.common.entrypoint import run_main
    run_main(main, "synthesize_result.json")
