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

DEFAULTS: dict[str, str] = {
    "WORKSPACE_DIR": "/workspace",
    "AI_MODEL": "claude-sonnet-4-6",
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
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "us.anthropic.claude-opus-4-6-v1",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "us.anthropic.claude-sonnet-4-6",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
}

# Short alias → canonical name.
# If the canonical name is already set, it takes precedence.
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
]


def _apply_aliases(env: dict[str, str]) -> None:
    """Fill canonical names from their aliases when the canonical is absent."""
    for alias, canonical in _ALIASES:
        if not env.get(canonical) and env.get(alias):
            env[canonical] = env[alias]


def load_config(required: list[str] | None = None) -> dict[str, str]:
    """Load config from env vars, applying defaults and prefix aliases.

    Exits with code 1 and a clear message if any required var is missing.
    """
    config = dict(DEFAULTS)
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
