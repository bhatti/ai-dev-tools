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
import sys
from datetime import date

from scripts.common.claude_runner import run_claude
from scripts.common.config import load_config, get_workspace_dir, validate_claude_config


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYNTHESIZE_PROMPT = """\
You are an expert engineering team lead generating an evidence-backed standup brief.

## Today's Date
{today}

## Gathered Signals (JSON)
```json
{signals_json}
```

## Instructions

Use the `/ygs-standup` skill logic and `/ygs-risk-scan` skill logic to produce the brief.
You have all the data above — do NOT call any external APIs.

### Step 1 — Per-person status
For each person with assigned issues across ALL boards in all_sprints[]:
- What did they complete or close?
- What are they currently working on (ISSUE-KEY + title)?
- Any blockers, stale items, or Slack signals?
- Every claim must trace to an issue key/PR number — never fabricate.
- If no issues assigned to someone: "No tracker activity in last {lookback_hours}h"
- If issues list is empty: say "No issues found in sprint" — do NOT invent work.

### Step 2 — Per-board status table
For each board in all_sprints[] (use all_sprints[].board and all_sprints[].name):
- Count: total issues, done, in-progress, not-started
- Days left until sprint end
- Any HIGH risks for that board

### Step 3 — Risk scan (apply ygs-risk-scan thresholds)
Rank risks:
- 🔴 HIGH — blocks another person's work OR sprint goal at risk if unaddressed today
  Triggers: issue stale >5d, PR open >4d no review, blocked label, dependency chain stale
- 🟡 MEDIUM — bad trajectory, becomes HIGH within 2 days
  Triggers: issue stale >3d, PR open >2d no review, person silent in standup >2d
- ℹ️ LOW — worth noting, not meeting time
- Every risk line MUST include the assignee's first name in parentheses: e.g. "KEY (@Shahzad): reason"

### Step 4 — Discussion questions
2-3 items requiring human judgment (not status recitation):
- Blocked items that need a decision
- Scope/priority trade-offs
- Team bottlenecks (single reviewer, single expert, etc.)
- Every discussion item MUST start with the Jira key and assignee's first name: "KEY (@Name): question"

### Step 5 — Output format

You MUST produce output in EXACTLY this structure with EXACTLY these two delimiter lines:

#### STANDUP_BRIEF
<Slack message here — plain text only, no markdown>
#### RISK_REPORT
<Full risk detail here — Markdown is fine>

Rules for STANDUP_BRIEF (this is the Slack message — plain text only):
- No asterisks, no underscores, no backticks, no bold/italic — plain readable text only
- No ## headings — use ALL CAPS labels like "BOARD STATUS", "STATUS", "RISKS", "DISCUSSION"
- Bullets: use • (plain text bullet)
- Keep entire Slack message under 1500 chars total
- Reference issues as KEY only (e.g. CRIBL-1234)

STANDUP_BRIEF must follow this exact template:
```
📋 Standup Brief — {today}

BOARD STATUS
• <board name> / <sprint name>: <N> total, <N> done, <N> in-progress, <N> not started — <N> days left

STATUS
• <Person>: working on KEY (summary). Completed: KEY. <blocker if any>

RISKS
🔴 KEY (@assignee): one-line reason
🟡 KEY (@assignee): one-line reason

DISCUSSION
1. KEY (@assignee): question requiring human decision
2. KEY (@assignee): question
```

#### RISK_REPORT (full detail, saved as artifact — standard Markdown is fine here)
Full ranked risk list with recommended actions, dependency graph summary, capacity numbers per board.

### Step 6 — Exit JSON (last line of output, required)
```json
{{"status":"DONE","risk_count":<N_high_and_medium>,"discussion_questions":<N>,"silence_count":<N_with_no_activity>}}
```
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
    """Remove any risk report content that leaked into the brief.

    Cuts at the first line that starts a risk report section or at the
    ```json status line, whichever comes first.
    """
    import re
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

    brief = _truncate_brief(_clean_code_fence(_extract_section(output, "STANDUP_BRIEF")))
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
