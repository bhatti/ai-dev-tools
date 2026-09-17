"""Parse skill invocation flags from RAW_ARGS and resolve repo/branch/tracker.

The RAW_ARGS string comes from the Slack router and has the format:
    <skill-name> [positional-args...] [--flags...] [-- additional instructions]

Two parsing modes:

Positional shorthand (no unrecognized --flags present):
  - First non-numeric token → repo (if --repo not set)
  - First numeric token → identifier (PR number, issue ID)
  - Remaining tokens → prepended to instructions
  Examples:
    review-pr myapp 4444                          → repo=myapp, id=4444
    review-pr myapp 4444 --branch release         → repo=myapp, id=4444, branch=release
    analyze myapp --branch dev -- focus on tests  → repo=myapp, instructions="focus on tests"

Passthrough mode (any unrecognized --flag detected):
  All tokens not consumed by a known framework flag (--repo/--branch/--tracker/
  --service/--model) are passed verbatim as instructions. This lets skills
  receive their own CLI-style flags naturally.
  Examples:
    run-tests unit --workers 4 --dry-run --branch feat
      → instructions="unit --workers 4 --dry-run", branch=feat
    my-skill --dry-run --count 5
      → instructions="--dry-run --count 5"
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class SkillFlags:
    skill: str = ""
    repo: str = ""
    branch: str = ""
    tracker: str = ""
    service: str = ""
    model: str = ""
    identifier: str = ""
    instructions: str = ""


_FLAG_PATTERN = re.compile(
    r"--(?P<key>repo|branch|tracker|service|model)\s+(?P<val>\S+)"
)

_NUMERIC = re.compile(r"^\d+$")

# Matches any --flag that is NOT one of the known framework flags.
# Presence of an unknown flag triggers passthrough mode (no positional parsing).
_UNKNOWN_FLAG_RE = re.compile(
    r"--(?!(?:repo|branch|tracker|service|model)(?:\s|$))\w"
)


def parse_skill_flags(raw: str) -> SkillFlags:
    """Parse a RAW_ARGS string into a SkillFlags dataclass.

    Two modes depending on whether unknown --flags are present:

    Passthrough mode (any unrecognized --flag found):
      All tokens that are not a known framework flag (--repo/--branch/--tracker/
      --service/--model) are passed verbatim as instructions. This lets skills
      receive their own CLI-style flags naturally, e.g.:
        run-tests unit --workers 4 --dry-run --branch feat

    Positional shorthand (no unrecognized --flags):
      - First non-numeric word → repo (unless --repo is also present)
      - First numeric word → identifier (PR/issue number)
      - Remaining words → prepended to instructions
      e.g.:  review-pr myapp 4444 --branch release
    """
    raw = (raw or "").strip()
    if not raw:
        return SkillFlags()

    # Split on " -- " to separate flags from additional instructions.
    parts = raw.split(" -- ", 1)
    flag_part = parts[0].strip()
    instructions = parts[1].strip() if len(parts) > 1 else ""

    # Extract skill name as the first token (before any --flag).
    tokens = flag_part.split()
    skill = ""
    rest_start = 0
    if tokens and not tokens[0].startswith("--"):
        skill = tokens[0]
        rest_start = 1

    # Rejoin remaining tokens for regex-based flag extraction.
    rest = " ".join(tokens[rest_start:])

    flags = SkillFlags(skill=skill, instructions=instructions)

    # Extract explicit known --key value flags.
    flag_spans: list[tuple[int, int]] = []
    for m in _FLAG_PATTERN.finditer(rest):
        key = m.group("key")
        val = m.group("val")
        flag_spans.append((m.start(), m.end()))
        if key == "repo":
            flags.repo = val
        elif key == "branch":
            flags.branch = val
        elif key == "tracker":
            flags.tracker = val.lower()
        elif key == "service":
            flags.service = val
        elif key == "model":
            flags.model = val

    # Build the remaining string after removing known-flag spans.
    remaining = ""
    if rest:
        consumed = set()
        for start, end in flag_spans:
            for i in range(start, end):
                consumed.add(i)
        remaining = "".join(c if i not in consumed else " " for i, c in enumerate(rest)).strip()

    # Passthrough mode: unknown --flags present → treat remainder as instructions verbatim.
    if _UNKNOWN_FLAG_RE.search(remaining):
        if remaining:
            flags.instructions = f"{remaining} {flags.instructions}".strip() if flags.instructions else remaining
        return flags

    # Positional shorthand: first non-numeric → repo, first numeric → identifier.
    extra_words: list[str] = []
    for token in remaining.split():
        if token == "--":
            continue
        if _NUMERIC.match(token) and not flags.identifier:
            flags.identifier = token
        elif not flags.repo and not token.startswith("-"):
            flags.repo = token
        else:
            extra_words.append(token)

    # Prepend leftover positional words to instructions.
    if extra_words:
        extra = " ".join(extra_words)
        flags.instructions = f"{extra} {flags.instructions}".strip() if flags.instructions else extra

    return flags


def resolve_tracker(flags: SkillFlags, config: dict) -> str:
    """Resolve effective tracker from flags, repo URL, and config.

    Priority:
      1. Explicit --tracker flag
      2. Auto-detect from --repo URL domain
      3. DEFAULT_TRACKER from org config
    """
    if flags.tracker:
        return flags.tracker

    if flags.repo:
        if "github.com" in flags.repo:
            return "github"
        if "bitbucket.org" in flags.repo:
            return "jira"

    return (config.get("DEFAULT_TRACKER") or "jira").lower().strip()


def resolve_repo(flags: SkillFlags, config: dict, tracker: str) -> tuple[str | None, str]:
    """Resolve clone URL and branch from flags + org config.

    Returns (clone_url, branch). clone_url is None when no repo is available.

    Resolution for repo:
      1. --repo flag (full URL or short name expanded via org config)
      2. CODEBASE_REPO_URL env var
      3. Org config defaults (GH_ORG+GH_REPO or BITBUCKET_WORKSPACE+BITBUCKET_REPO)
      4. None (skill runs without a codebase)

    Resolution for branch:
      1. --branch flag
      2. GIT_BRANCH env var
      3. Org config branch (GitHubRepoBranch or BitbucketRepoBranch)
      4. Default: "main" for github, "dev" for jira/bitbucket
    """
    clone_url = _resolve_clone_url(flags, config, tracker)
    branch = _resolve_branch(flags, config, tracker)
    return clone_url, branch


def _resolve_clone_url(flags: SkillFlags, config: dict, tracker: str) -> str | None:
    repo = flags.repo

    if repo:
        if repo.startswith("http://") or repo.startswith("https://") or repo.startswith("git@"):
            return repo
        if "/" in repo:
            return _expand_org_repo(repo, tracker)
        return _expand_bare_name(repo, config, tracker)

    env_url = config.get("CODEBASE_REPO_URL", "").strip()
    if env_url and env_url not in ("<no value>", "{{.CodebaseRepoUrl}}"):
        return env_url

    return _url_from_org_config(config, tracker)


def _expand_org_repo(slug: str, tracker: str) -> str:
    """Expand 'org/repo' to full URL based on tracker."""
    if tracker == "github":
        return f"https://github.com/{slug}.git"
    return f"https://bitbucket.org/{slug}.git"


def _expand_bare_name(name: str, config: dict, tracker: str) -> str | None:
    """Expand bare repo name (e.g., 'myapp') to full URL using org config."""
    if tracker == "github":
        org = config.get("GH_ORG", "").strip()
        if org:
            return f"https://github.com/{org}/{name}.git"
    else:
        workspace = config.get("BITBUCKET_WORKSPACE", "").strip()
        if workspace:
            return f"https://bitbucket.org/{workspace}/{name}.git"
    return None


def _url_from_org_config(config: dict, tracker: str) -> str | None:
    """Build clone URL from org config defaults (GH_ORG+GH_REPO or BB vars)."""
    if tracker == "github":
        org = config.get("GH_ORG", "").strip()
        repo = config.get("GH_REPO", "").strip()
        if org and repo:
            return f"https://github.com/{org}/{repo}.git"
    else:
        workspace = config.get("BITBUCKET_WORKSPACE", "").strip()
        repo = config.get("BITBUCKET_REPO", "").strip()
        if workspace and repo:
            return f"https://bitbucket.org/{workspace}/{repo}.git"
    return None


def _resolve_branch(flags: SkillFlags, config: dict, tracker: str) -> str:
    if flags.branch:
        return flags.branch

    env_branch = config.get("GIT_BRANCH", "").strip()
    if env_branch and env_branch not in ("<no value>", "{{.GitBranch}}"):
        return env_branch

    if tracker == "github":
        return config.get("GH_REPO_BRANCH", "main").strip() or "main"
    return config.get("BB_REPO_BRANCH", "dev").strip() or "dev"
