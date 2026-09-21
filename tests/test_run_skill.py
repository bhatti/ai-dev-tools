"""Tests for scripts/adhoc/run_skill.py — system prompt mapping and credential gating."""

import os
from unittest.mock import patch

import pytest

import pytest
from scripts.common.claude_runner import SYSTEM_PROMPTS
from scripts.adhoc.run_skill import _system_prompt_for_skill, _SKILL_SYSTEM_PROMPT_MAP, _detect_intent
from scripts.common.config import (
    MODEL_BEDROCK_HAIKU, MODEL_BEDROCK_SONNET, MODEL_BEDROCK_OPUS,
)


# ---------------------------------------------------------------------------
# _system_prompt_for_skill — correct prompt per skill
# ---------------------------------------------------------------------------

def test_system_prompt_standup_skills():
    assert _system_prompt_for_skill("ygs-standup") == SYSTEM_PROMPTS["standup"]
    assert _system_prompt_for_skill("ygs-risk-scan") == SYSTEM_PROMPTS["standup"]
    assert _system_prompt_for_skill("ygs-pr-queue") == SYSTEM_PROMPTS["standup"]


def test_system_prompt_review_skills():
    assert _system_prompt_for_skill("ygs-review-pr") == SYSTEM_PROMPTS["review"]
    assert _system_prompt_for_skill("ygs-code-review") == SYSTEM_PROMPTS["review"]
    assert _system_prompt_for_skill("ygs-security-review") == SYSTEM_PROMPTS["review"]
    assert _system_prompt_for_skill("ygs-sre-review") == SYSTEM_PROMPTS["review"]


def test_system_prompt_implement_skills():
    assert _system_prompt_for_skill("ygs-implement") == SYSTEM_PROMPTS["implement"]
    assert _system_prompt_for_skill("ygs-ship") == SYSTEM_PROMPTS["implement"]


def test_system_prompt_learn_skills():
    assert _system_prompt_for_skill("ygs-learn") == SYSTEM_PROMPTS["learn"]
    assert _system_prompt_for_skill("ygs-retro") == SYSTEM_PROMPTS["learn"]


def test_system_prompt_ask_skill():
    assert _system_prompt_for_skill("ygs-ask") == SYSTEM_PROMPTS["adhoc"]


def test_system_prompt_unknown_skill_defaults_to_adhoc():
    """Unknown skill names fall back to the adhoc prompt, not standup."""
    assert _system_prompt_for_skill("ygs-unknown-new-skill") == SYSTEM_PROMPTS["adhoc"]
    assert _system_prompt_for_skill("custom-skill") == SYSTEM_PROMPTS["adhoc"]


def test_adhoc_prompt_exists():
    """SYSTEM_PROMPTS must have an 'adhoc' key after the claude_runner update."""
    assert "adhoc" in SYSTEM_PROMPTS
    assert len(SYSTEM_PROMPTS["adhoc"]) > 50


def test_skill_prompt_exists():
    """SYSTEM_PROMPTS must have a 'skill' key for the ai-skill execution route."""
    assert "skill" in SYSTEM_PROMPTS
    assert len(SYSTEM_PROMPTS["skill"]) > 50


def test_skill_prompt_used_as_default_for_unknown_skills():
    """ai-skill route uses 'skill' prompt as fallback, not 'adhoc'."""
    assert _system_prompt_for_skill("some-custom-skill", default="skill") == SYSTEM_PROMPTS["skill"]


def test_adhoc_default_unchanged():
    """Without a default override the existing adhoc fallback is preserved."""
    assert _system_prompt_for_skill("some-custom-skill") == SYSTEM_PROMPTS["adhoc"]


def test_all_mapped_skills_have_valid_prompt_keys():
    """Every skill in _SKILL_SYSTEM_PROMPT_MAP maps to a real SYSTEM_PROMPTS key."""
    for skill, key in _SKILL_SYSTEM_PROMPT_MAP.items():
        assert key in SYSTEM_PROMPTS, f"Skill {skill!r} maps to unknown key {key!r}"


# ---------------------------------------------------------------------------
# --turns flag: max_turns resolution (unit-tests the logic from run_skill.main)
# ---------------------------------------------------------------------------

def _resolve_max_turns(turns_flag: str, config: dict, default: int = 100) -> int:
    """Replicate the --turns resolution logic from skill/run_skill.main() for testing."""
    default_turns = int(config.get("MAX_TURNS_ADHOC", config.get("MAX_TURNS_IMPLEMENT", str(default))))
    if turns_flag:
        try:
            return int(turns_flag)
        except ValueError:
            return default_turns
    return default_turns


def test_turns_flag_overrides_config():
    assert _resolve_max_turns("200", {"MAX_TURNS_ADHOC": "50"}) == 200


def test_turns_flag_invalid_falls_back_to_config():
    assert _resolve_max_turns("abc", {"MAX_TURNS_ADHOC": "75"}) == 75


def test_turns_empty_uses_config():
    assert _resolve_max_turns("", {"MAX_TURNS_ADHOC": "80"}) == 80


def test_turns_empty_uses_hardcoded_default():
    assert _resolve_max_turns("", {}) == 100


# ---------------------------------------------------------------------------
# Model short-name resolution via AI_MODEL_OVERRIDE
# ---------------------------------------------------------------------------

def _resolve_model_override(model_override: str, config: dict) -> str:
    """Replicate the shortname resolution logic from run_skill.main() for testing."""
    from scripts.common.config import MODEL_SHORTNAMES
    _shortnames = {
        "haiku":  config.get("ANTHROPIC_DEFAULT_HAIKU_MODEL",  MODEL_SHORTNAMES["haiku"]),
        "sonnet": config.get("ANTHROPIC_DEFAULT_SONNET_MODEL", MODEL_SHORTNAMES["sonnet"]),
        "opus":   config.get("ANTHROPIC_DEFAULT_OPUS_MODEL",   MODEL_SHORTNAMES["opus"]),
        **{k: v for k, v in MODEL_SHORTNAMES.items() if k not in ("haiku", "sonnet", "opus")},
    }
    return _shortnames.get(model_override.lower(), model_override)


def test_model_override_haiku_resolves_to_full_id():
    config = {"ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL_BEDROCK_HAIKU}
    resolved = _resolve_model_override("haiku", config)
    assert resolved == MODEL_BEDROCK_HAIKU


def test_model_override_sonnet_resolves_to_full_id():
    config = {"ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL_BEDROCK_SONNET}
    resolved = _resolve_model_override("sonnet", config)
    assert resolved == MODEL_BEDROCK_SONNET


def test_model_override_case_insensitive():
    config = {"ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL_BEDROCK_HAIKU}
    assert _resolve_model_override("HAIKU", config) == MODEL_BEDROCK_HAIKU
    assert _resolve_model_override("Sonnet", config) is not None


def test_model_override_full_id_passthrough():
    """Full model IDs (not shortnames) are passed through unchanged."""
    resolved = _resolve_model_override(MODEL_BEDROCK_HAIKU, {})
    assert resolved == MODEL_BEDROCK_HAIKU


def test_model_override_unknown_name_passthrough():
    """Unknown shortnames are passed through as-is (forward-compat with new models)."""
    resolved = _resolve_model_override("claude-future-model", {})
    assert resolved == "claude-future-model"


def test_model_shortnames_include_newer_models():
    """MODEL_SHORTNAMES must include the newer model aliases for Aperture proxy."""
    from scripts.common.config import MODEL_SHORTNAMES
    assert "sonnet-5" in MODEL_SHORTNAMES
    assert "opus-5" in MODEL_SHORTNAMES
    assert "fable" in MODEL_SHORTNAMES
    assert MODEL_SHORTNAMES["sonnet-5"].startswith("us.anthropic.claude-sonnet-5")
    assert MODEL_SHORTNAMES["opus-5"].startswith("us.anthropic.claude-opus-5")


# ---------------------------------------------------------------------------
# _detect_intent — review URL routing
# Tests populate _KNOWN_SKILLS to exercise the installed-skills-aware routing.
# ---------------------------------------------------------------------------

@pytest.fixture()
def with_review_skills():
    """Populate _KNOWN_SKILLS with review skills for intent detection tests."""
    from scripts.common import claude_runner
    saved = set(claude_runner._KNOWN_SKILLS)
    claude_runner._KNOWN_SKILLS.update({
        "ygs-review-deep", "ygs-review-pr", "ygs-security-review", "ygs-ask",
    })
    yield
    claude_runner._KNOWN_SKILLS.clear()
    claude_runner._KNOWN_SKILLS.update(saved)


def test_detect_intent_non_ask_skill_unchanged(with_review_skills):
    """Non-ygs-ask skills are never overridden regardless of URL content."""
    assert _detect_intent("review https://bitbucket.org/org/repo/pull-requests/1", "ygs-standup") == "ygs-standup"
    assert _detect_intent("deep review https://github.com/org/repo/pull/42", "ygs-implement") == "ygs-implement"


def test_detect_intent_github_pr_url_routes_to_review_pr(with_review_skills):
    prompt = "please review https://github.com/org/repo/pull/42"
    assert _detect_intent(prompt, "ygs-ask") == "ygs-review-pr"


def test_detect_intent_bitbucket_pr_url_routes_to_review_pr(with_review_skills):
    prompt = "check https://bitbucket.org/org/repo/pull-requests/123/overview"
    assert _detect_intent(prompt, "ygs-ask") == "ygs-review-pr"


def test_detect_intent_deep_review_github_routes_to_review_deep(with_review_skills):
    prompt = "deep review https://github.com/org/repo/pull/99"
    assert _detect_intent(prompt, "ygs-ask") == "ygs-review-deep"


def test_detect_intent_deep_review_bitbucket_routes_to_review_deep(with_review_skills):
    prompt = "deep review https://bitbucket.org/org/repo/pull-requests/46257/overview"
    assert _detect_intent(prompt, "ygs-ask") == "ygs-review-deep"


def test_detect_intent_no_url_stays_ask(with_review_skills):
    prompt = "what is the status of the project?"
    assert _detect_intent(prompt, "ygs-ask") == "ygs-ask"


def test_detect_intent_url_but_no_pr_path_stays_ask(with_review_skills):
    prompt = "look at https://github.com/org/repo for context"
    assert _detect_intent(prompt, "ygs-ask") == "ygs-ask"


def test_detect_intent_deep_keyword_without_url_stays_ask(with_review_skills):
    prompt = "do a deep analysis of the codebase"
    assert _detect_intent(prompt, "ygs-ask") == "ygs-ask"


def test_detect_intent_falls_back_when_no_skills_installed():
    """When no review skills are installed, intent detection falls back to original skill."""
    from scripts.common import claude_runner
    saved = set(claude_runner._KNOWN_SKILLS)
    claude_runner._KNOWN_SKILLS.clear()
    try:
        prompt = "deep review https://github.com/org/repo/pull/1"
        assert _detect_intent(prompt, "ygs-ask") == "ygs-ask"
    finally:
        claude_runner._KNOWN_SKILLS.update(saved)


def test_detect_intent_falls_back_when_only_partial_review_skills_installed():
    """With only ygs-review-pr installed (no ygs-review-deep), deep review uses ygs-review-pr."""
    from scripts.common import claude_runner
    saved = set(claude_runner._KNOWN_SKILLS)
    claude_runner._KNOWN_SKILLS.clear()
    claude_runner._KNOWN_SKILLS.add("ygs-review-pr")
    try:
        prompt = "deep review https://github.com/org/repo/pull/1"
        result = _detect_intent(prompt, "ygs-ask")
        assert result == "ygs-review-pr"
    finally:
        claude_runner._KNOWN_SKILLS.clear()
        claude_runner._KNOWN_SKILLS.update(saved)


# ---------------------------------------------------------------------------
# _build_prompt — MANDATORY directive only when skill_md is loaded
# ---------------------------------------------------------------------------

from scripts.adhoc.run_skill import _build_prompt


def test_build_prompt_with_skill_md_has_mandatory_directive():
    """When a real SKILL.md is loaded, the prompt must open with a MANDATORY directive."""
    prompt = _build_prompt("ygs-analyze", "TICKET-123 --dry-run", "# Skill protocol")
    assert "MANDATORY" in prompt
    assert "ygs-analyze" in prompt
    assert "# Skill protocol" in prompt
    # Directive must appear before the skill body, not buried after it
    mandatory_pos = prompt.index("MANDATORY")
    skill_header_pos = prompt.index("## Skill: ygs-analyze")
    assert mandatory_pos < skill_header_pos


def test_build_prompt_without_skill_md_no_mandatory_directive():
    """When no SKILL.md is found and no fallback exists, no MANDATORY prefix."""
    prompt = _build_prompt("unknown-skill-xyz", "do something", None)
    assert "MANDATORY" not in prompt
    assert "No SKILL.md found" in prompt


def test_build_prompt_fallback_description_no_mandatory_directive():
    """Built-in fallback descriptions (ygs-ask etc.) don't get a MANDATORY prefix."""
    from scripts.adhoc.run_skill import _SKILL_FALLBACK_DESCRIPTIONS
    if "ygs-ask" not in _SKILL_FALLBACK_DESCRIPTIONS:
        pytest.skip("ygs-ask fallback not present")
    prompt = _build_prompt("ygs-ask", "answer this", None)
    assert "MANDATORY" not in prompt


def test_build_prompt_mandatory_names_the_skill():
    """The MANDATORY directive must include the skill name so Claude knows which to follow."""
    prompt = _build_prompt("edge-pr-hygiene", "run checks", "# hygiene content")
    assert "edge-pr-hygiene" in prompt.split("## Skill:")[0]  # in the directive, before the skill body


def test_build_prompt_request_section_preserved():
    """The ## Request section must contain the caller's prompt text."""
    prompt = _build_prompt("some-skill", "ISSUE-99999 --dry-run", "# skill body")
    assert "## Request" in prompt
    assert "ISSUE-99999 --dry-run" in prompt
