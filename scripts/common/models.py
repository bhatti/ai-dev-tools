"""Claude model selection: the single place that turns a name into a model id.

Defaults live in ``models.json`` next to this file (edit that file to roll a model or
change the default). Every script resolves through :func:`resolve_model`; workflows do
not define models.

Precedence (first non-empty wins):
    1. ``requested``  - an explicit per-run choice (Slack ``--model``, ``AiModel`` param)
    2. ``env_model``  - ``AI_MODEL`` set on the job
    3. ``tier``       - a role such as ``low`` (router/classify) or ``high``
    4. the file's ``default``

A name may be an alias from the file (``sonnet``, ``high``, ``low``...; case-insensitive)
or a full model id. Anything else raises :class:`ModelError` listing the valid aliases,
so a typo fails at start-up instead of as a model-not-found error mid-job.

CLI::

    python3 -m scripts.common.models --table
    python3 -m scripts.common.models --tier high
    python3 -m scripts.common.models --resolve opus
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

MODELS_FILE = Path(__file__).with_name("models.json")

# Values a workflow template leaves behind when a parameter was not supplied.
_UNSET_MARKERS = {"", "<no value>", "<nil>", "None"}

# A full model id (Bedrock "us.anthropic.claude-...", direct API "claude-..."), not a free-form word.
_FULL_ID_RE = re.compile(r"^(?=.*claude)[A-Za-z0-9][A-Za-z0-9._:/-]*$")

# ANTHROPIC_DEFAULT_*_MODEL variable -> alias it carries (what the claude CLI maps "sonnet" etc. to).
_CLAUDE_ENV_ALIASES = {
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "sonnet",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "opus",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "haiku",
}


class ModelError(ValueError):
    """The model file is invalid, or a requested model is neither an alias nor a model id."""


def _is_unset(value: str | None) -> bool:
    text = (value or "").strip()
    return text in _UNSET_MARKERS or text.startswith("{{")


def load_models() -> dict:
    """Read and validate the model file. A broken file is an error, never a silent fallback."""
    path = MODELS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ModelError(f"cannot read model defaults {path}: {e}") from e
    aliases, tiers, default = data.get("aliases"), data.get("tiers"), data.get("default")
    if not isinstance(aliases, dict) or not isinstance(tiers, dict) or not isinstance(default, str):
        raise ModelError(f"{path}: needs 'default' (string), 'tiers' (object) and 'aliases' (object)")
    for name, model_id in aliases.items():
        if not isinstance(model_id, str) or not _FULL_ID_RE.match(model_id):
            raise ModelError(f"{path}: alias '{name}' is not a model id: {model_id!r}")
    for name, target in {"default": default, **tiers}.items():
        if target not in aliases:
            raise ModelError(f"{path}: '{name}' points at unknown alias '{target}'")
    for needed in ("sonnet", "opus", "haiku"):
        if needed not in aliases:
            raise ModelError(f"{path}: alias '{needed}' is required (the claude CLI maps it)")
    return {"default": default, "tiers": {k.lower(): v for k, v in tiers.items()},
            "aliases": {k.lower(): v for k, v in aliases.items()}}


def _lookup(name: str, models: dict) -> str:
    key = name.strip().lower()
    if key in models["aliases"]:
        return models["aliases"][key]
    if key in models["tiers"]:
        return models["aliases"][models["tiers"][key]]
    if _FULL_ID_RE.match(name.strip()):
        return name.strip()
    valid = sorted(set(models["aliases"]) | set(models["tiers"]))
    raise ModelError(f"unknown model '{name}': use an alias ({', '.join(valid)}) or a full model id")


def resolve_model(requested: str = "", env_model: str = "", tier: str = "") -> str:
    """Return the model id for this run (see the module docstring for the precedence)."""
    models = load_models()
    for candidate in (requested, env_model):
        if not _is_unset(candidate):
            return _lookup(candidate, models)
    if not _is_unset(tier):
        key = tier.strip().lower()
        if key not in models["tiers"]:
            raise ModelError(f"unknown model tier '{tier}': use one of {', '.join(sorted(models['tiers']))}")
        return models["aliases"][models["tiers"][key]]
    return models["aliases"][models["default"]]


def resolve_run_model(config: dict, requested: str = "") -> str:
    """Model for one run: ``requested`` (Slack ``--model``) or the ``AI_MODEL_OVERRIDE`` job parameter, else the configured ``AI_MODEL``."""
    return resolve_model(requested=requested or os.environ.get("AI_MODEL_OVERRIDE", ""), env_model=config.get("AI_MODEL", ""))


def model_env() -> dict[str, str]:
    """ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU}_MODEL for the claude CLI and its settings.json."""
    aliases = load_models()["aliases"]
    return {var: aliases[alias] for var, alias in _CLAUDE_ENV_ALIASES.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show or resolve the configured Claude models")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tier", help="print the model id for a tier (default, low, medium, high)")
    group.add_argument("--resolve", help="print the model id for an alias or full id")
    group.add_argument("--table", action="store_true", help="print the defaults, tiers and aliases (default)")
    args = parser.parse_args(argv)
    try:
        if args.tier is not None:
            print(resolve_model(tier=args.tier))
        elif args.resolve is not None:
            print(resolve_model(requested=args.resolve))
        else:
            models = load_models()
            print(f"default: {models['default']} -> {models['aliases'][models['default']]}")
            for tier, alias in sorted(models["tiers"].items()):
                print(f"tier {tier}: {alias} -> {models['aliases'][alias]}")
            for alias, model_id in sorted(models["aliases"].items()):
                print(f"alias {alias}: {model_id}")
    except ModelError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
