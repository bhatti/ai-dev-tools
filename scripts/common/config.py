"""Configuration loader with validation.

Reads environment variables, applies defaults, validates required vars.
Each script calls load_config(required=[...]) at startup.

Accepted env var prefixes
-------------------------
GitHub:    GH_*    or  GITHUB_*     (e.g. GH_TOKEN  == GITHUB_TOKEN)
BitBucket: BB_*    or  BITBUCKET_*  (e.g. BB_REPO   == BITBUCKET_REPO)

The canonical internal names are always the longer forms (GH_*, BITBUCKET_*).
Short aliases are resolved once at load time; scripts never need to check both.
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

# ---------------------------------------------------------------------------
# Model ID constants — single source of truth for all scripts and YAML defaults.
# Update these to roll to a new model version; no other files need changing.
# Callers may still override at runtime via ANTHROPIC_DEFAULT_*_MODEL env vars.
# ---------------------------------------------------------------------------
MODEL_BEDROCK_SONNET = "us.anthropic.claude-sonnet-4-6"
MODEL_BEDROCK_OPUS   = "us.anthropic.claude-opus-4-6-v1"
MODEL_BEDROCK_HAIKU  = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MODEL_SONNET         = "claude-sonnet-4-6"              # direct Anthropic API (non-Bedrock)
MODEL_HAIKU          = "claude-haiku-4-5-20251001-v1:0" # direct Anthropic API (non-Bedrock)

# Short name → full model ID, used by "using <name> model" message syntax.
# Covers Bedrock versions and common Aperture proxy aliases.
# Unknown names are passed through as-is, so full model IDs always work.
# Complexity-tiered model selection — used by the implement pipeline.
# The plan task writes plan_complexity.txt (low/medium/high); the implement task
# reads it and selects the corresponding model.  Override via org configs
# (AnthropicComplexityLowModel / AnthropicComplexityHighModel) or models.env.
COMPLEXITY_MODEL_MAP: dict[str, str] = {
    "low":    MODEL_BEDROCK_HAIKU,   # simple tasks — fast and cheap
    "medium": MODEL_BEDROCK_SONNET,  # default
    "high":   MODEL_BEDROCK_OPUS,    # complex architecture changes
}

MODEL_SHORTNAMES: dict[str, str] = {
    # Current defaults (resolved from env vars at runtime in run_skill.py)
    "haiku":     MODEL_BEDROCK_HAIKU,
    "sonnet":    MODEL_BEDROCK_SONNET,
    "opus":      MODEL_BEDROCK_OPUS,
    # Newer Bedrock versions available through Aperture proxy
    "sonnet-5":  "us.anthropic.claude-sonnet-5",
    "opus-5":    "us.anthropic.claude-opus-5",
    "opus-4-8":  "us.anthropic.claude-opus-4-8",
    "fable":     "anthropic.claude-fable-5",
    "fable-5":   "anthropic.claude-fable-5",
    "haiku-4-5": MODEL_BEDROCK_HAIKU,
    # Mantle variants (no us. prefix)
    "sonnet-5-mantle": "anthropic.claude-sonnet-5",
    "opus-5-mantle":   "anthropic.claude-opus-5",
}

DEFAULTS: dict[str, str] = {
    "WORKSPACE_DIR": "/workspace",
    "AI_MODEL": MODEL_SONNET,
    "MAX_TURNS_PLAN": "50",
    "MAX_TURNS_IMPLEMENT": "100",
    "PICKUP_LABEL": "ai-ready",
    "INPROGRESS_LABEL": "ai-in-progress",
    "PR_OPEN_LABEL": "ai-pr-open",
    "NEEDS_HUMAN_LABEL": "needs-human",
    "GIT_USER_NAME": "AI Agent",
    "GIT_USER_EMAIL": "ai-agent@noreply.local",
    "MAX_ISSUES": "5",
    "POLL_INTERVAL": "120",
    "ANTHROPIC_BEDROCK_BASE_URL": "http://ai/bedrock",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
    "ANTHROPIC_DEFAULT_OPUS_MODEL":   MODEL_BEDROCK_OPUS,
    "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL_BEDROCK_SONNET,
    "ANTHROPIC_DEFAULT_HAIKU_MODEL":  MODEL_BEDROCK_HAIKU,
}

# Short alias → canonical name.
# If the canonical name is already set, it takes precedence.
# Two alias groups:
#   1. Short env var prefixes (BB_*, GITHUB_*) → canonical long forms
#   2. Formicary org config CamelCase names → canonical UPPER_SNAKE env var names
#      Formicary normally injects org config via YAML templates, but these aliases let
#      scripts handle CamelCase values when passed directly as params or config dicts.
_ALIASES: list[tuple[str, str]] = [
    # GitHub: GITHUB_* → GH_*
    ("GITHUB_TOKEN",     "GH_TOKEN"),
    ("GITHUB_ORG",       "GH_ORG"),
    ("GITHUB_REPO",      "GH_REPO"),
    # BitBucket: BB_* → BITBUCKET_*
    ("BB_TOKEN",         "BITBUCKET_TOKEN"),
    ("BB_USERNAME",      "BITBUCKET_USERNAME"),
    ("BB_WORKSPACE",     "BITBUCKET_WORKSPACE"),
    ("BB_REPO",          "BITBUCKET_REPO"),
    # Formicary org config CamelCase → canonical env vars
    ("BitbucketToken",              "BITBUCKET_TOKEN"),
    ("BitbucketUsername",           "BITBUCKET_USERNAME"),
    ("BitbucketWorkspace",          "BITBUCKET_WORKSPACE"),
    ("BitbucketRepo",               "BITBUCKET_REPO"),
    ("GitHubToken",                 "GH_TOKEN"),
    ("GitHubOrg",                   "GH_ORG"),
    ("GitHubRepo",                  "GH_REPO"),
    ("JiraUrl",                     "JIRA_BASE_URL"),
    ("JiraProject",                 "JIRA_PROJECT"),
    ("JiraSpace",                   "JIRA_SPACE"),
    ("JiraTeamField",               "JIRA_TEAM_FIELD"),
    ("JiraBoards",                  "JIRA_BOARDS"),
    ("JiraEmail",                   "JIRA_EMAIL"),
    ("JiraApiToken",                "JIRA_API_TOKEN"),
    ("SlackToken",                  "SLACK_BOT_TOKEN"),
    ("SlackChannel",                "SLACK_CHANNEL"),
    ("SlackThreadTs",               "SLACK_THREAD_TS"),
    ("DefaultTracker",              "DEFAULT_TRACKER"),
    ("StandupTeamMembers",          "STANDUP_TEAM_MEMBERS"),
    ("StandupLookbackHours",        "STANDUP_LOOKBACK_HOURS"),
    ("StandupStaleDays",            "STANDUP_STALE_DAYS"),
    ("ClaudeUseBedrock",            "CLAUDE_CODE_USE_BEDROCK"),
    ("ClaudeSkipBedrockAuth",       "CLAUDE_CODE_SKIP_BEDROCK_AUTH"),
    ("AnthropicBedrockBaseUrl",     "ANTHROPIC_BEDROCK_BASE_URL"),
    ("AnthropicSonnetModel",        "ANTHROPIC_DEFAULT_SONNET_MODEL"),
    ("AnthropicOpusModel",          "ANTHROPIC_DEFAULT_OPUS_MODEL"),
    ("AnthropicHaikuModel",         "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
    ("AnthropicComplexityLowModel", "ANTHROPIC_COMPLEXITY_LOW_MODEL"),
    ("AnthropicComplexityHighModel","ANTHROPIC_COMPLEXITY_HIGH_MODEL"),
    ("MaxClaudeProcessTimeout",     "MAX_CLAUDE_PROCESS_TIMEOUT"),
    ("ExtraSkillsRepos",            "EXTRA_SKILLS_REPOS"),
    ("GitUserName",                 "GIT_USER_NAME"),
    ("GitUserEmail",                "GIT_USER_EMAIL"),
    ("FormicaryUrl",                "FORMICARY_URL"),
    ("FormicaryPublicURL",          "FORMICARY_PUBLIC_URL"),
    ("CodebaseDir",                 "CODEBASE_DIR"),
    ("CodebaseRepoUrl",             "CODEBASE_REPO_URL"),
    ("GitBranch",                   "GIT_BRANCH"),
    ("AiModel",                     "AI_MODEL_OVERRIDE"),
]


def _apply_aliases(env: dict[str, str]) -> None:
    """Fill canonical names from their aliases when the canonical is absent."""
    for alias, canonical in _ALIASES:
        if not env.get(canonical) and env.get(alias):
            env[canonical] = env[alias]


def _load_dotenv(env: dict[str, str]) -> None:
    """Load KEY=VALUE pairs from a .env file in the current directory.

    Only sets keys that are NOT already in the environment — env vars always win.
    Lines starting with '#' and blank lines are ignored.
    """
    dotenv_path = Path(".env")
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            env.setdefault(key, value)


def load_config(required: list[str] | None = None) -> dict[str, str]:
    """Load config from env vars (.env file then OS env), applying defaults and aliases.

    Load order (later wins for env vars):
    1. DEFAULTS
    2. .env in current directory (only for keys absent from OS env)
    3. OS environment variables

    Exits with code 1 and a clear message if any required var is missing.
    """
    config = dict(DEFAULTS)
    _load_dotenv(config)   # .env values fill gaps before OS env overrides
    config.update(os.environ)
    _apply_aliases(config)

    if required:
        missing = [k for k in required if not config.get(k)]
        if missing:
            print(f"ERROR: Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)

    return config


def validate_claude_config(config: dict[str, str]) -> None:
    """Verify Claude API credentials are present before invoking the claude CLI.

    Exits with code 1 and a diagnostic message if neither ANTHROPIC_API_KEY
    nor CLAUDE_CODE_USE_BEDROCK=1 is configured.  Prints the active mode so
    it appears in the formicary task log at startup.
    """
    use_bedrock = config.get("CLAUDE_CODE_USE_BEDROCK", "0") == "1"
    api_key = config.get("ANTHROPIC_API_KEY", "")

    if use_bedrock:
        base_url = config.get("ANTHROPIC_BEDROCK_BASE_URL", "http://ai/bedrock")
        parsed = urlparse(base_url)
        # Strip userinfo (user:password@) so credentials are never logged.
        safe_url = urlunparse(parsed._replace(netloc=parsed.hostname or ""))
        print(f"[claude] mode=bedrock base_url={safe_url}")
    elif api_key:
        print("[claude] mode=direct-api-key")
    else:
        print(
            "ERROR: Claude API not configured.\n"
            "  Set CLAUDE_CODE_USE_BEDROCK=1 (with optional ANTHROPIC_BEDROCK_BASE_URL)\n"
            "  or set ANTHROPIC_API_KEY for direct API access.",
            file=sys.stderr,
        )
        sys.exit(1)


def get_workspace_dir(config: dict[str, str]) -> Path:
    """Return the workspace root directory."""
    return Path(config["WORKSPACE_DIR"])


def get_issue_dir(config: dict[str, str], issue_id: str) -> Path:
    """Return the issue workspace directory.

    Each task runs in its own pod with a fresh emptyDir at /workspace, so
    there is never more than one issue per container. Always return workspace
    directly — no issue-id subdirectory needed.
    """
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace
