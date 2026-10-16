"""Tests for scripts/common/models.py + models.json and for every caller that resolves a model."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.common import models as m
from scripts.common.models import ModelError, load_models, model_env, resolve_model

SONNET = "us.anthropic.claude-sonnet-5-5"
OPUS = "us.anthropic.claude-opus-4-6-v1"
HAIKU = "anthropic.claude-haiku-4-5"


class TestShippedFile:
    def test_is_valid_and_has_the_agreed_defaults(self):
        data = load_models()
        assert data["aliases"][data["default"]] == SONNET
        assert [resolve_model(tier=t) for t in ("default", "high", "low")] == [SONNET, OPUS, HAIKU]
        assert resolve_model(tier="medium") == SONNET

    def test_every_alias_is_a_model_id(self):
        for name, model_id in load_models()["aliases"].items():
            assert m._FULL_ID_RE.match(model_id), name

    def test_model_env_covers_the_cli_aliases(self):
        assert model_env() == {"ANTHROPIC_DEFAULT_SONNET_MODEL": SONNET, "ANTHROPIC_DEFAULT_OPUS_MODEL": OPUS,
                               "ANTHROPIC_DEFAULT_HAIKU_MODEL": HAIKU}


class TestPrecedence:
    def test_default_when_nothing_given(self):
        assert resolve_model() == SONNET

    def test_tier_beats_default(self):
        assert resolve_model(tier="low") == HAIKU

    def test_env_model_beats_tier(self):
        assert resolve_model(env_model="opus", tier="low") == OPUS

    def test_request_beats_env_model_and_tier(self):
        assert resolve_model(requested="haiku", env_model="opus", tier="high") == HAIKU

    @pytest.mark.parametrize("unset", ["", "  ", "<no value>", "{{.AiModel}}", "None"])
    def test_unset_markers_fall_through(self, unset):
        assert resolve_model(requested=unset, env_model=unset, tier="high") == OPUS


class TestNames:
    @pytest.mark.parametrize("name,expected", [("SONNET", SONNET), ("Opus", OPUS), ("low", HAIKU), ("high", OPUS),
                                               ("default", SONNET), ("fable", "anthropic.claude-fable-5")])
    def test_aliases_and_tiers_are_case_insensitive(self, name, expected):
        assert resolve_model(requested=name) == expected

    @pytest.mark.parametrize("model_id", ["us.anthropic.claude-opus-4-6-v1", "claude-sonnet-4-6",
                                          "us.anthropic.claude-haiku-4-5-20251001-v1:0"])
    def test_full_ids_pass_through(self, model_id):
        assert resolve_model(requested=model_id) == model_id

    @pytest.mark.parametrize("bad", ["opuss", "gpt-4", "sonnet; rm -rf /", "--model"])
    def test_unknown_names_fail_with_the_valid_aliases(self, bad):
        with pytest.raises(ModelError) as e:
            resolve_model(requested=bad)
        assert "sonnet" in str(e.value) and "opus" in str(e.value)

    def test_unknown_tier_fails(self):
        with pytest.raises(ModelError, match="tier"):
            resolve_model(tier="ultra")


class TestFileValidation:
    def _write(self, tmp_path, data):
        p = tmp_path / "models.json"
        p.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return p

    GOOD = {"default": "sonnet", "tiers": {"high": "opus"},
            "aliases": {"sonnet": "a.claude-s", "opus": "a.claude-o", "haiku": "a.claude-h"}}

    def test_good_file_loads(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "MODELS_FILE", self._write(tmp_path, self.GOOD))
        assert load_models()["aliases"]["opus"] == "a.claude-o"

    @pytest.mark.parametrize("mutate,msg", [
        (lambda d: d.update(default="nope"), "unknown alias"),
        (lambda d: d["tiers"].update(low="nope"), "unknown alias"),
        (lambda d: d["aliases"].update(sonnet="not a model"), "not a model id"),
        (lambda d: d["aliases"].pop("haiku"), "required"),
        (lambda d: d.pop("tiers"), "needs"),
    ])
    def test_bad_files_are_rejected(self, tmp_path, monkeypatch, mutate, msg):
        data = json.loads(json.dumps(self.GOOD))
        mutate(data)
        monkeypatch.setattr(m, "MODELS_FILE", self._write(tmp_path, data))
        with pytest.raises(ModelError, match=msg):
            load_models()

    def test_missing_and_malformed_files_are_errors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "MODELS_FILE", tmp_path / "absent.json")
        with pytest.raises(ModelError):
            load_models()
        monkeypatch.setattr(m, "MODELS_FILE", self._write(tmp_path, "{not json"))
        with pytest.raises(ModelError):
            load_models()


class TestCli:
    def run(self, *args):
        return subprocess.run([sys.executable, "-m", "scripts.common.models", *args], capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parent.parent)

    def test_tier_and_resolve_print_just_the_id(self):
        assert self.run("--tier", "high").stdout.strip() == OPUS
        assert self.run("--resolve", "low").stdout.strip() == HAIKU

    def test_unknown_exits_2_with_message(self):
        r = self.run("--resolve", "opuss")
        assert r.returncode == 2 and "unknown model" in r.stderr and r.stdout == ""

    def test_table_lists_defaults(self):
        out = self.run("--table").stdout
        assert "default: sonnet" in out and "tier high: opus" in out


class TestCallers:
    @pytest.fixture(autouse=True)
    def _no_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # load_config reads ./.env

    def test_load_config_defaults_to_the_json_default(self, monkeypatch):
        from scripts.common.config import load_config
        for k in ("AI_MODEL", "AI_MODEL_TIER"):
            monkeypatch.delenv(k, raising=False)
        assert load_config()["AI_MODEL"] == SONNET

    def test_load_config_honours_tier_and_alias_and_ignores_placeholders(self, monkeypatch):
        from scripts.common.config import load_config
        monkeypatch.delenv("AI_MODEL", raising=False)
        monkeypatch.setenv("AI_MODEL_TIER", "low")
        assert load_config()["AI_MODEL"] == HAIKU
        monkeypatch.setenv("AI_MODEL", "opus")
        assert load_config()["AI_MODEL"] == OPUS
        monkeypatch.setenv("AI_MODEL", "<no value>")
        assert load_config()["AI_MODEL"] == HAIKU

    def test_load_config_exits_on_a_bad_model(self, monkeypatch, capsys):
        from scripts.common.config import load_config
        monkeypatch.setenv("AI_MODEL", "opuss")
        with pytest.raises(SystemExit) as e:
            load_config()
        assert e.value.code == 1 and "unknown model" in capsys.readouterr().err

    def test_stale_org_config_names_no_longer_select_models(self, monkeypatch):
        from scripts.common.config import load_config
        for k in ("AI_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("AnthropicSonnetModel", "us.anthropic.claude-sonnet-4-6")
        cfg = load_config()
        assert cfg["AI_MODEL"] == SONNET and "ANTHROPIC_DEFAULT_SONNET_MODEL" not in cfg

    def test_skill_flag_overrides_config(self):
        from scripts.skill.flags import parse_skill_flags
        from scripts.skill.run_skill import _resolve_model
        cfg = {"AI_MODEL": SONNET}
        assert _resolve_model(cfg, parse_skill_flags("ask --model high what is up")) == OPUS
        assert _resolve_model(cfg, parse_skill_flags("ask what is up")) == SONNET

    def test_skill_override_env_param(self, monkeypatch):
        from scripts.skill.flags import parse_skill_flags
        from scripts.skill.run_skill import _resolve_model
        monkeypatch.setenv("AI_MODEL_OVERRIDE", "low")
        assert _resolve_model({"AI_MODEL": SONNET}, parse_skill_flags("ask hi")) == HAIKU
        monkeypatch.setenv("AI_MODEL_OVERRIDE", "{{.AiModel}}")
        assert _resolve_model({"AI_MODEL": SONNET}, parse_skill_flags("ask hi")) == SONNET

    def test_skill_bad_model_raises(self):
        from scripts.skill.flags import parse_skill_flags
        from scripts.skill.run_skill import _resolve_model
        with pytest.raises(ModelError):
            _resolve_model({"AI_MODEL": SONNET}, parse_skill_flags("ask --model opuss hi"))

    def test_claude_env_carries_json_models_not_job_values(self, monkeypatch):
        from scripts.common.claude_runner import build_claude_env
        monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "stale-job-value")
        assert build_claude_env()["ANTHROPIC_DEFAULT_SONNET_MODEL"] == SONNET

    def test_settings_json_is_created_and_refreshed(self, tmp_path):
        from scripts.common.claude_runner import write_bedrock_settings
        p = tmp_path / ".claude" / "settings.json"
        write_bedrock_settings(p)
        assert json.loads(p.read_text())["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == OPUS
        stale = json.loads(p.read_text())
        stale["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "old"
        stale["enabledPlugins"] = {"x": True}
        p.write_text(json.dumps(stale))
        write_bedrock_settings(p)
        now = json.loads(p.read_text())
        assert now["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == OPUS and now["enabledPlugins"] == {"x": True}
