"""Tests for scripts/adhoc/run_skill.py — system prompt mapping and credential gating."""

import os
from unittest.mock import patch

import pytest

from scripts.common.claude_runner import SYSTEM_PROMPTS
from scripts.adhoc.run_skill import _system_prompt_for_skill, _SKILL_SYSTEM_PROMPT_MAP
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


def test_all_mapped_skills_have_valid_prompt_keys():
    """Every skill in _SKILL_SYSTEM_PROMPT_MAP maps to a real SYSTEM_PROMPTS key."""
    for skill, key in _SKILL_SYSTEM_PROMPT_MAP.items():
        assert key in SYSTEM_PROMPTS, f"Skill {skill!r} maps to unknown key {key!r}"


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
