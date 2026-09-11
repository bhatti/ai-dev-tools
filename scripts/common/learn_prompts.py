"""Shared prompt templates for learn.py (both jira and gh variants)."""

from scripts.common.health_check_prompts import HEALTH_CHECK_DIMS

# Shared body — inserted after the issue header line in each variant.
# Placeholders: {pr_context}, {impl_summary}, {comments_text}, {title}
_LEARN_PROMPT_BODY = (
    "\n"
    "## PR Data\n"
    "{pr_context}\n"
    "\n"
    "## Implementation Summary\n"
    "{impl_summary}\n"
    "\n"
    "## PR Comments\n"
    "{comments_text}\n"
    "\n"
    "## Phase 0: PR Health Check\n"
    "Analyze this single PR using the dimensions below. Be brief — 1-3 bullets per section.\n"
    "Do NOT do cross-PR pattern analysis. Omit any dimension where nothing is noteworthy.\n"
    "\n"
    + HEALTH_CHECK_DIMS
    + "\n"
    "Write findings under \"## PR Health Analysis\" in learnings.md.\n"
    "\n"
    "## Phase 1: Implementation Learnings\n"
    "Invoke /ygs-learn to extract actionable learnings from the PR comments above:\n"
    "1. What patterns worked well?\n"
    "2. What conventions or project-specific patterns were encountered?\n"
    "3. What should be done differently next time?\n"
    "4. Any surprises or non-obvious findings?\n"
    "\n"
    "## Output\n"
    "Write learnings.md with this structure:\n"
    "  # Post-Merge Analysis: {title}\n"
    "  ## PR Health Analysis\n"
    "  [Phase 0 findings]\n"
    "  ## Implementation Learnings\n"
    "  [Phase 1 learnings]\n"
    "\n"
    "Output ONLY this JSON on the last line:\n"
    '{{"status":"DONE","learning_count":<N>,"health_signals":<M>}}\n'
)

# Jira variant: issue header uses "PROJ-42: Title" style
JIRA_LEARN_PROMPT_TEMPLATE = (
    "You are an expert software engineer producing a combined post-merge analysis.\n\n"
    "## {issue_id}: {title}"
    + _LEARN_PROMPT_BODY
)

# GitHub variant: issue header uses "Issue #42: Title" style
GH_LEARN_PROMPT_TEMPLATE = (
    "You are an expert software engineer producing a combined post-merge analysis.\n\n"
    "## Issue #{issue_id}: {title}"
    + _LEARN_PROMPT_BODY
)
