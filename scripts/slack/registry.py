"""Registry loader for Formicary Slack router.

Loads ``workflows.yml`` and ``skills.yml`` from the same directory as this
module and provides intent-resolution helpers used by the router.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class WorkflowEntry:
    name: str
    job_type: str
    shape: str
    triggers: list[str]
    skill: str
    id_var: str
    required_vars: list[str]
    target_kind: str  # "jira" | "github" | "any"
    description: str
    prompt: str = ""  # canned prompt for ai-adhoc entries; overrides free-text from message
    cron: bool = False  # if True, trigger the existing PENDING request instead of submitting


@dataclass
class SkillEntry:
    name: str
    path: str
    ref: str
    description: str
    source: str = ""


# ---------------------------------------------------------------------------
# Heuristics for inferring target_kind from an entity string
# ---------------------------------------------------------------------------

_JIRA_ISSUE_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")  # e.g. PROJ-123
_GITHUB_PR_URL_RE = re.compile(r"github\.com/.+/pull/\d+", re.IGNORECASE)
_GITHUB_ISSUE_URL_RE = re.compile(r"github\.com/.+/issues/\d+", re.IGNORECASE)
_BB_PR_URL_RE = re.compile(r"bitbucket\.org/.+/pull-requests/\d+", re.IGNORECASE)


def _infer_target_kind(entity_id: str) -> str:
    """Heuristically guess target_kind from entity_id string."""
    if not entity_id:
        return "any"
    if _JIRA_ISSUE_RE.match(entity_id.strip()):
        return "jira"
    if _GITHUB_PR_URL_RE.search(entity_id) or _GITHUB_ISSUE_URL_RE.search(entity_id):
        return "github"
    if _BB_PR_URL_RE.search(entity_id):
        return "jira"  # Bitbucket PRs are treated as jira-side reviews
    return "any"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class Registry:
    """Holds loaded workflow and skill entries; resolves user intents to entries."""

    def __init__(self, workflows_path: Path, skills_path: Path) -> None:
        with open(workflows_path, "r", encoding="utf-8") as fh:
            wdata = yaml.safe_load(fh) or {}
        with open(skills_path, "r", encoding="utf-8") as fh:
            sdata = yaml.safe_load(fh) or {}

        self.workflows: list[WorkflowEntry] = [
            WorkflowEntry(
                name=w.get("name", ""),
                job_type=w.get("job_type", ""),
                shape=w.get("shape", ""),
                triggers=[t.lower() for t in w.get("triggers", [])],
                skill=w.get("skill", ""),
                id_var=w.get("id_var", ""),
                required_vars=w.get("required_vars", []),
                target_kind=w.get("target_kind", "any"),
                description=w.get("description", ""),
                prompt=w.get("prompt", ""),
                cron=bool(w.get("cron", False)),
            )
            for w in wdata.get("workflows", [])
        ]

        self.skills: list[SkillEntry] = [
            SkillEntry(
                name=s.get("name", ""),
                path=s.get("path", ""),
                ref=s.get("ref", "main"),
                description=s.get("description", ""),
                source=s.get("source", ""),
            )
            for s in sdata.get("skills", [])
        ]

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------

    def resolve(
        self,
        intent: str,
        target_kind: str = "any",
        default_tracker: str = "any",
    ) -> Optional[WorkflowEntry]:
        """Find the best matching workflow for *intent* and *target_kind*.

        Matching rules (in priority order):
        1. Exact target_kind match where intent matches a trigger.
        2. default_tracker match (when target_kind is "any" and default_tracker is set).
        3. target_kind="any" entry where intent matches a trigger.
        4. First match regardless of target_kind (fallback).
        """
        intent_lower = intent.lower().strip()
        exact: list[WorkflowEntry] = []
        default_match: list[WorkflowEntry] = []
        any_kind: list[WorkflowEntry] = []
        fallback: list[WorkflowEntry] = []

        for entry in self.workflows:
            matched = any(intent_lower in trigger for trigger in entry.triggers)
            if not matched:
                matched = any(trigger in intent_lower for trigger in entry.triggers)
            if not matched:
                continue
            if entry.target_kind == target_kind:
                exact.append(entry)
            elif target_kind == "any" and default_tracker != "any" and entry.target_kind == default_tracker:
                default_match.append(entry)
            elif entry.target_kind == "any":
                any_kind.append(entry)
            else:
                fallback.append(entry)

        return (exact or default_match or any_kind or fallback or [None])[0]

    def parse_verb(
        self, verb: str, rest: str
    ) -> Optional[tuple[str, str, str]]:
        """Parse a structured command into (intent, entity_id, target_kind).

        ``verb`` is the command word (e.g. "review", "implement", "standup").
        ``rest`` is everything after the verb (e.g. a PR URL or Jira key).

        Returns None if the verb is not recognised.
        """
        verb_lower = verb.lower().strip()
        entity_id = rest.strip()

        # Map common verb aliases to canonical intents
        verb_to_intent: dict[str, str] = {
            "review": "review",
            "pr": "review",
            "implement": "implement",
            "build": "implement",
            "standup": "standup",
            "status": "standup",
            "daily": "standup",
            "risk": "risk",
            "risks": "risk scan",
            "security": "security review",
            "sre": "sre review",
            "ops": "ops review",
            "prs": "pr queue",
            "queue": "pr queue",
            "pulls": "pr queue",
            "comments": "pr comments",
            "feedback": "pr feedback",
            "tasks": "pr tasks",
            "help": "__help__",
            "commands": "__help__",
            "?": "__help__",
        }

        intent = verb_to_intent.get(verb_lower)
        if intent is None:
            return None

        target_kind = _infer_target_kind(entity_id)
        return (intent, target_kind, entity_id)

    def help_message(self, bot_name: str = "@bot") -> str:
        """Return a plain-text help message listing available commands and how to extend.

        ``bot_name`` is the display name used in examples (e.g. "@mybot").
        """
        lines: list[str] = [f"*Available commands* (mention {bot_name} to use them):", ""]

        # Deduplicate: one entry per unique first-trigger, skip help aliases
        seen_triggers: set[str] = set()
        for entry in self.workflows:
            primary = entry.triggers[0] if entry.triggers else entry.name
            if primary in seen_triggers:
                continue
            seen_triggers.add(primary)
            aliases = "  |  ".join(f"`{bot_name} {t}`" for t in entry.triggers[:3])
            lines.append(f"• {aliases}")
            lines.append(f"  _{entry.description}_")

        lines += [
            "",
            "*Adding a new skill*",
            "1. Create `skills/ygs-<name>/SKILL.md` in <https://github.com/bhatti/you-got-skills|you-got-skills> (define what Claude does)",
            "2. Add an entry to `scripts/slack/skills.yml` (name, path, description)",
            "3. Add an entry to `scripts/slack/workflows.yml` (triggers, job_type, skill name)",
            "4. If the skill needs a new Formicary workflow, add a YAML under `docs/examples/` and run `deploy-ai-jira-workflows.sh`",
            "5. No router code changes needed — trigger words drive routing",
            "",
            f"_Tip: ad-hoc skills that just run a prompt and reply in Slack need only `job_type: ai-adhoc` with a `prompt:` field — no new workflow YAML needed._",
        ]
        return "\n".join(lines)

    def missing_required_vars(
        self, entry: WorkflowEntry, target_id: str
    ) -> list[str]:
        """Return required variables not satisfied by *target_id*.

        *target_id* is considered to satisfy ``entry.id_var``.
        """
        satisfied = {entry.id_var} if target_id else set()
        return [v for v in entry.required_vars if v not in satisfied]

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_default(cls) -> Registry:
        """Load registries from the same directory as this module file."""
        here = Path(__file__).parent
        return cls(
            workflows_path=here / "workflows.yml",
            skills_path=here / "skills.yml",
        )
