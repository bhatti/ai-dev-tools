"""Parse skill invocation flags from RAW_ARGS and resolve repo/branch/tracker.

The RAW_ARGS string comes from the Slack router and has the format:
    <skill-name> [--repo <url|name>] [--branch <name>] [--tracker github|jira]
                  [--model <id>] [--service <image:tag>] [-- additional instructions]

Examples:
    ygs-analyze --repo myapp --branch dev -- focus on test coverage
    ygs-security-review --repo https://github.com/org/repo --branch release-2.0
    ygs-investigate -- investigate flaky test JIRA-123
    ygs-qa --repo myapp --service myapp/myapp-leader:4.10 -- run E2E tests
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class SkillFlags:
    skill: str = ""
    repo: str = ""
    branch: str = ""
    tracker: str = ""
    service: str = ""
    model: str = ""
    instructions: str = ""


_FLAG_PATTERN = re.compile(
    r"--(?P<key>repo|branch|tracker|service|model)\s+(?P<val>\S+)"
)


def parse_skill_flags(raw: str) -> SkillFlags:
    """Parse a RAW_ARGS string into a SkillFlags dataclass."""
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

    for m in _FLAG_PATTERN.finditer(rest):
        key = m.group("key")
        val = m.group("val")
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
