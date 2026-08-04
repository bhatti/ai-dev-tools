"""Synthesize standup brief from gathered signals using Claude + ygs-standup skill.

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
import re
import sys
from datetime import date

from scripts.common.claude_runner import run_claude
from scripts.common.config import load_config, get_workspace_dir, validate_claude_config


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYNTHESIZE_PROMPT = """\
You are an expert engineering team lead generating a concise, evidence-backed standup brief.

TODAY: {today}

SIGNALS:
{signals_json}

TASK: Produce a standup brief. Do NOT call any tools or APIs. Use only the data above.

===== OUTPUT FORMAT — STRICTLY ENFORCED =====

Output MUST start with exactly:

#### STANDUP_BRIEF

Then later:

#### RISK_REPORT

STANDUP_BRIEF RULES (any violation = wrong answer):
1. NO markdown: no **, no __, no `, no #, no >, no ->, no →
2. Bullets: • only (never -)
3. Section headers ALL CAPS on their own line: BOARD STATUS, STATUS, RISKS, DISCUSSION
4. RISKS: emoji prefix only (🔴 🟡), no bold, no arrows
5. DISCUSSION: numbered 1. 2. 3.
6. Max 1800 chars total

EXACT TEMPLATE (fill in data, keep structure):

Standup Brief — {today}

BOARD STATUS
• <SprintName> (<FirstName, FirstName, ...>): <N> total, <N> done, <N> in-prog/review, <N> not started — ends <date or "today">

STATUS
• <FirstName>: <issues in-progress/review as KEY, KEY>. PRs <NNN/NNN> open <Nd> <reviewer status>. <stale/blocked notes>.
(one • per person, 150 chars max, include PR numbers when associated with their issues)

RISKS
🔴 KEY (@FirstName): <reason, PR status if open> — max 100 chars
🟡 KEY (@FirstName): <reason> — max 100 chars

DISCUSSION
1. KEY (@FirstName): <decision needed today>

===== ANALYSIS STEPS =====

Step 1 — BOARD STATUS:
  Use all_sprints[]. One bullet per sprint: name, team (first names), counts by status category, end date.

Step 2 — STATUS (primary sprint — most issues):
  One bullet per person. For each person's open/in-progress issues:
  - List issue keys
  - If the issue has an open PR in open_prs[] (match by title containing the key), include PR number and age
  - Show reviewer status: "no reviewers", "N approved", "N pending"
  - Flag stale (>3d no update) and BLOCKED

Step 3 — RISKS:
  🔴 HIGH: P0/P1 with open PR >3d no reviewers, BLOCKED, sprint ending today with unfinished P0/P1
  🟡 MEDIUM: P2 stale >3d, open PR >2d no reviewers, sprint ending today with open work

Step 4 — DISCUSSION (2-3 items needing human decision today only):

#### RISK_REPORT
Full ranked risk report with details. Standard markdown OK here.

Exit JSON (last line, required):
{{"status":"DONE","risk_count":<N>,"discussion_questions":<N>,"silence_count":<N>}}
"""


def _build_prompt(signals: dict) -> str:
    lookback = signals.get("config_summary", {}).get("lookback_hours", 26)
    # Trim comments to keep prompt size manageable (Bedrock has strict input limits)
    trimmed = json.loads(json.dumps(signals))
    for issue in trimmed.get("issues", []):
        comments = issue.get("recent_comments", [])
        # Keep only 3 most recent comments, truncate long bodies
        short = []
        for c in comments[-3:]:
            text = c.get("text", "")
            short.append({**c, "text": text[:300] + ("..." if len(text) > 300 else "")})
        issue["recent_comments"] = short
    # Trim Slack message text
    for msg in trimmed.get("slack_messages", []):
        if len(msg.get("text", "")) > 200:
            msg["text"] = msg["text"][:200] + "..."

    return _SYNTHESIZE_PROMPT.format(
        today=date.today().isoformat(),
        signals_json=json.dumps(trimmed, indent=2),
        lookback_hours=lookback,
    )


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def _extract_section(text: str, heading: str) -> str:
    """Extract content after '#### HEADING' up to the next '####' or end."""
    marker = f"#### {heading}"
    start = text.find(marker)
    if start == -1:
        return ""
    start += len(marker)
    next_section = text.find("####", start)
    return text[start:next_section].strip() if next_section != -1 else text[start:].strip()


def _clean_code_fence(text: str) -> str:
    """Strip leading/trailing markdown code fences."""
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _truncate_brief(text: str) -> str:
    """Remove any risk report content that leaked into the brief."""
    stop_patterns = [
        r"^#{1,4}\s+RISK",        # ## RISK REPORT heading
        r"^RISK REPORT",           # plain heading
        r"^---\s*$",               # horizontal rule separator
        r"^```json",               # status JSON fence
    ]
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for pat in stop_patterns:
            if re.match(pat, line.strip(), re.IGNORECASE):
                return "\n".join(lines[:i]).strip()
    return text


def _strip_markdown(text: str) -> str:
    """Remove common markdown formatting that breaks Slack plain-text display.

    Slack renders * as a literal asterisk, so **bold** shows as **bold**.
    Strip bold/italic markers, inline code, heading prefixes, and arrow chars.
    """
    # Remove bold/italic markers: **, __, *, _
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    # Remove inline code backticks
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Remove markdown heading prefixes (## Foo → FOO already handled by prompt, but clean up any)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Replace markdown dashes at line start with •
    text = re.sub(r'^\s*-\s+', '• ', text, flags=re.MULTILINE)
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

    prompt = _build_prompt(signals)

    try:
        result = run_claude(
            prompt,
            working_dir=workspace_dir,
            model=config.get("AI_MODEL"),
            max_turns=int(config.get("MAX_TURNS_STANDUP", "30")),
            log_file=workspace_dir / "logs" / "synthesize.log",
        )
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        (workspace_dir / "synthesize_result.json").write_text(
            json.dumps({"status": "ERROR", "reason": str(e)})
        )
        sys.exit(1)

    output = result.output

    brief = _strip_markdown(_truncate_brief(_clean_code_fence(_extract_section(output, "STANDUP_BRIEF"))))
    risk_report = _clean_code_fence(_extract_section(output, "RISK_REPORT"))

    # Fallback: if sections not found, use the full output as the brief (truncated)
    if not brief:
        brief = _truncate_brief(output.strip())

    (workspace_dir / "standup_brief.md").write_text(brief)
    if risk_report:
        (workspace_dir / "risk_report.md").write_text(risk_report)

    (workspace_dir / "synthesize_result.json").write_text(
        json.dumps(result.status_json or {"status": result.status}, indent=2)
    )

    sj = result.status_json or {}
    if result.status == "DONE":
        print(
            f"[synthesize] brief written — risks={sj.get('risk_count', '?')} "
            f"questions={sj.get('discussion_questions', '?')}",
            flush=True,
        )
    else:
        print(f"[synthesize] unexpected status '{result.status}'", flush=True)

    sys.exit(0)


if __name__ == "__main__":
    from scripts.common.entrypoint import run_main
    run_main(main, "synthesize_result.json")
