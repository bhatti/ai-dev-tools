"""Invoke Claude Code CLI and parse structured output.

Wraps the `claude` CLI with proper flags, captures output,
and extracts the JSON status line from the response.

Formicary artifact API (for debugging job output):
  # List artifacts for a job
  curl -sk -H "Authorization: Bearer ${FORMICARY_TOKEN}" \
    "${FORMICARY_URL}/api/artifacts?job_request_id=<JOB_ID}" | python3 -m json.tool

  # Download an artifact by its SHA-256 digest
  curl -sk -H "Authorization: Bearer ${FORMICARY_TOKEN}" \
    "${FORMICARY_URL}/api/artifacts/<SHA256>/download" -o output.txt

  # Get job details (state, error_message, task results)
  curl -sk -H "Authorization: Bearer ${FORMICARY_TOKEN}" \
    "${FORMICARY_URL}/api/jobs/requests/<JOB_ID>" | python3 -m json.tool

  # Get console log for a specific task execution
  # 1. Get the task execution SHA from job details (.task_executions[].tasks[].console_sha)
  # 2. curl -sk -H "Authorization: Bearer ${FORMICARY_TOKEN}" \
  #      "${FORMICARY_URL}/api/artifacts/<SHA>/download"

Note: FORMICARY_URL uses https with a self-signed cert on nip.io deployments — always pass -sk.
"""

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from scripts.common.config import (
    MODEL_BEDROCK_HAIKU as _MODEL_BEDROCK_HAIKU,
    MODEL_BEDROCK_OPUS as _MODEL_BEDROCK_OPUS,
    MODEL_BEDROCK_SONNET as _MODEL_BEDROCK_SONNET,
)
from scripts.common.git_utils import clone_repo

# Cached across multiple run_claude() calls in the same pod — YGS only cloned once.
_YGS_INSTALLED: bool = False
# Markdown list of installed skills injected into every run_claude() system prompt.
_SKILLS_INVENTORY: str = ""
# Set of known skill names for SKILLS_INVOKED detection; populated by _ensure_ygs_skills()
# and extended by _ensure_extra_skills().
_KNOWN_SKILLS: set[str] = set()


def _ensure_ygs_skills() -> None:
    """Clone you-got-skills and symlink skills into ~/.claude/skills/ if not already done.

    Formicary overrides the container ENTRYPOINT with its own shell, so the
    entrypoint.sh YGS install step is bypassed.  This function replicates that
    logic from Python so Claude always has skills available regardless of how
    the container started.
    """
    global _YGS_INSTALLED
    if _YGS_INSTALLED:
        return

    home = Path.home()
    skills_base = home / ".claude" / "skills"
    install_dir = skills_base / "you-got-skills"
    skills_base.mkdir(parents=True, exist_ok=True)

    if not install_dir.exists():
        import time as _time
        _t0 = _time.monotonic()
        print("[ygs] cloning you-got-skills...", flush=True)
        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "1",
                 "https://github.com/bhatti/you-got-skills.git",
                 str(install_dir)],
                capture_output=True, text=True,
                timeout=90,  # hard cap — prevents hanging on slow/blocked network
            )
        except subprocess.TimeoutExpired:
            elapsed = _time.monotonic() - _t0
            print(f"[ygs] WARNING: clone timed out after {elapsed:.0f}s — proceeding without skills",
                  file=sys.stderr, flush=True)
            _YGS_INSTALLED = True
            return
        elapsed = _time.monotonic() - _t0
        if result.returncode != 0:
            print(f"[ygs] WARNING: clone failed after {elapsed:.0f}s: {result.stderr.strip()}",
                  file=sys.stderr, flush=True)
            _YGS_INSTALLED = True  # don't retry on every call
            return
        print(f"[ygs] clone completed in {elapsed:.0f}s", flush=True)

    # Symlink each skill and build inventory for system-prompt injection
    ygs_skills_dir = install_dir / "skills"
    count = 0
    skill_names: list[str] = []
    inventory_lines: list[str] = []
    for skill_dir in sorted(ygs_skills_dir.glob("ygs-*")):
        if skill_dir.is_dir():
            link = skills_base / skill_dir.name
            link.unlink(missing_ok=True)
            link.symlink_to(skill_dir.resolve())
            count += 1
            skill_names.append(skill_dir.name)
            try:
                first_line = (skill_dir / "SKILL.md").read_text(errors="replace").split("\n")[0].lstrip("# ").strip()
            except OSError:
                first_line = ""
            inventory_lines.append(f"- `/{skill_dir.name}` — {first_line}")
    print(f"[ygs] {count} skills installed → {skills_base}/", flush=True)
    print(f"::add-task-context YGS_SKILLS_COUNT::{count}")
    if skill_names:
        print(f"::add-task-context YGS_SKILLS_INSTALLED::{','.join(skill_names)}")
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(install_dir), "rev-parse", "--short", "HEAD"],
            text=True, timeout=5,
        ).strip()
    except Exception:
        commit = "unknown"
    print(f"::add-task-context YGS_SKILLS_REPO_URL::https://github.com/bhatti/you-got-skills.git")
    print(f"::add-task-context YGS_SKILLS_REPO_COMMIT::{commit}")
    global _SKILLS_INVENTORY, _KNOWN_SKILLS
    _KNOWN_SKILLS.update(skill_names)
    if inventory_lines:
        _SKILLS_INVENTORY = (
            "## Available Skills\n"
            "Use the most applicable skill below. If none fits, proceed without one.\n\n"
            + "\n".join(inventory_lines)
        )

    # Apply project-level skill overrides if CODEBASE_DIR is set
    codebase_dir = os.environ.get("CODEBASE_DIR", "")
    if codebase_dir:
        proj_skills = Path(codebase_dir) / ".claude" / "skills"
        if proj_skills.is_dir():
            for skill_dir in sorted(proj_skills.iterdir()):
                if skill_dir.is_dir():
                    link = skills_base / skill_dir.name
                    link.unlink(missing_ok=True)
                    link.symlink_to(skill_dir.resolve())
            print(f"[ygs] project skill overrides applied from {proj_skills}", flush=True)

    # Write ~/.claude/settings.json if missing — entrypoint.sh does this but
    # Formicary overrides the ENTRYPOINT so it never runs in task pods.
    settings_path = home / ".claude" / "settings.json"
    if not settings_path.exists() and os.environ.get("CLAUDE_CODE_USE_BEDROCK", "0") == "1":
        import json as _json
        settings = {
            "claudeCodeLocalNetworkingEnabled": True,
            "apiKeyHelper": "echo '-'",
            "env": {
                "ANTHROPIC_BEDROCK_BASE_URL": os.environ.get(
                    "ANTHROPIC_BEDROCK_BASE_URL", "http://ai/bedrock"
                ),
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "CLAUDE_CODE_SKIP_BEDROCK_AUTH": os.environ.get(
                    "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "1"
                ),
                "ANTHROPIC_DEFAULT_OPUS_MODEL": os.environ.get(
                    "ANTHROPIC_DEFAULT_OPUS_MODEL", _MODEL_BEDROCK_OPUS
                ),
                "ANTHROPIC_DEFAULT_SONNET_MODEL": os.environ.get(
                    "ANTHROPIC_DEFAULT_SONNET_MODEL", _MODEL_BEDROCK_SONNET
                ),
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": os.environ.get(
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL", _MODEL_BEDROCK_HAIKU
                ),
            },
            "skipWorkflowUsageWarning": True,
            "permissions": {"allow": ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)"]},
        }
        settings_path.write_text(_json.dumps(settings, indent=2), encoding="utf-8")
        print("[ygs] wrote ~/.claude/settings.json (Bedrock)", flush=True)

    _ensure_extra_skills(skills_base)
    _YGS_INSTALLED = True


def _install_via_skills_cli(repo_ref: str, skills_base: Path) -> None:
    """Install skills using `npx skills add <repo>` (vercel-labs/skills CLI).

    Installs directly into ~/.claude/skills/ — vercel-labs/skills always uses this path;
    there is no --target override. repo_ref can be org/repo, a full GitHub URL, or any
    source the skills CLI accepts. GH_TOKEN is forwarded for private GitHub repos.
    Does NOT support sparse checkout — avoid for very large repos.
    """
    import time as _time
    _t0 = _time.monotonic()
    print(f"[ygs] installing skills via skills-cli: {repo_ref}", flush=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    gh_token = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    if gh_token:
        env["GITHUB_TOKEN"] = gh_token
        env["GH_TOKEN"] = gh_token
    try:
        result = subprocess.run(
            ["npx", "--yes", "skills", "add", repo_ref, "--agent", "claude-code", "--yes"],
            capture_output=True, text=True, timeout=120, env=env,
        )
        elapsed = _time.monotonic() - _t0
        if result.returncode != 0:
            print(
                f"[ygs] WARNING: skills-cli failed for {repo_ref} ({elapsed:.0f}s): "
                f"{result.stderr.strip()[-300:]}",
                file=sys.stderr, flush=True,
            )
        else:
            print(f"[ygs] skills-cli installed {repo_ref} in {elapsed:.0f}s", flush=True)
    except subprocess.TimeoutExpired:
        print(f"[ygs] WARNING: skills-cli timed out for {repo_ref}", file=sys.stderr, flush=True)
    except FileNotFoundError:
        print("[ygs] WARNING: npx not found — cannot use skills-cli type", file=sys.stderr, flush=True)


def _ensure_extra_skills(skills_base: Path) -> None:
    """Clone extra skill repos listed in EXTRA_SKILLS_REPOS env var.

    EXTRA_SKILLS_REPOS accepts three formats:

    1. Plain URL or name (YAML-safe, use for job params):
           https://github.com/bhatti/you-got-skills.git

    2. Comma/newline-separated list (YAML-safe, multiple repos):
           https://github.com/org/repo.git,skills-cli:nutlope/hallmark

       The "skills-cli:" prefix forces `npx skills add` for that entry.

    3. JSON array (full control; use for org config — JSON breaks YAML template substitution):
        [
          {
            "url": "https://bitbucket.org/org/repo.git",
            "branch": "main",
            "skills_dir": ".skills",  # subpath inside repo containing skill dirs
            "sparse": true,           # optional: sparse-checkout (default: true)
            "type": "skills-cli"      # optional: use npx skills add instead of git clone
          }
        ]

    Each repo is shallow-cloned (or sparse-checked-out) once into
    ~/.claude/skills/_extra_<slug>/, and every skill directory inside
    skills_dir is symlinked under skills_base.

    sparse=true is strongly recommended for large repos — only the skills_dir
    subtree is downloaded, making the clone take 1-3s instead of 10-30s.

    Credentials are auto-detected from env vars:
    - bitbucket.org: uses BITBUCKET_USERNAME + BITBUCKET_TOKEN (already in ai-dev-credentials secret)
    - github.com:    uses GH_TOKEN
    Override per-repo with explicit "token_env" / "username_env" fields if needed.

    Alternatively, set type="skills-cli" to use `npx skills add <repo>` from the
    vercel-labs/skills CLI (https://github.com/vercel-labs/skills). This handles
    auth, multiple agents, and skill format validation automatically. Requires
    Node.js/npx in the container (already present in plexobject/ai-dev-tools).
    NOTE: skills-cli does NOT support sparse checkout — avoid for very large repos.
    """
    import time as _time

    raw = os.environ.get("EXTRA_SKILLS_REPOS", "").strip()
    if not raw or raw in ("[]", ""):
        return

    # Accept:
    #   1. JSON array  — full control (recommended for org config, not YAML template params)
    #   2. Comma/newline-separated list of plain URLs, org/repo slugs, or bare names
    #      (safe for YAML template substitution since no JSON special chars)
    # Plain name resolution uses DEFAULT_TRACKER + existing workspace env vars.
    try:
        repos = json.loads(raw)
        if not isinstance(repos, list):
            repos = [repos]
    except (json.JSONDecodeError, ValueError):
        # Split on commas or newlines to support multiple repos without JSON syntax
        parts = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
        repos = parts if len(parts) > 1 else [raw]

    # Normalize plain strings or dicts missing "url" into full repo objects.
    # Plain strings support an optional "skills-cli:" prefix to force the skills-cli
    # install path: e.g. "skills-cli:nutlope/hallmark" or "skills-cli:org/repo"
    normalized: list[dict] = []
    tracker = os.environ.get("DEFAULT_TRACKER", "jira").lower()
    for entry in repos:
        if isinstance(entry, str):
            name = entry.strip()
            _force_skills_cli = False
            if name.startswith("skills-cli:"):
                _force_skills_cli = True
                name = name[len("skills-cli:"):]
            if name.startswith("http://") or name.startswith("https://") or name.startswith("git@"):
                # Already a full URL — wrap as a minimal dict
                entry = {"url": name}
                if _force_skills_cli:
                    entry["type"] = "skills-cli"
            elif "/" in name:
                workspace, repo_name = name.split("/", 1)
                if _force_skills_cli or tracker == "github":
                    url = f"https://github.com/{workspace}/{repo_name}.git"
                else:
                    url = f"https://bitbucket.org/{workspace}/{repo_name}.git"
                entry = {"url": url}
                if _force_skills_cli:
                    entry["type"] = "skills-cli"
            else:
                # Bare name — expand using BITBUCKET_WORKSPACE or GH_ORG
                if tracker == "github":
                    gh_org = os.environ.get("GH_ORG", "")
                    if not gh_org:
                        print(f"[ygs] WARNING: cannot expand '{name}' — GH_ORG not set", file=sys.stderr, flush=True)
                        continue
                    entry = {"url": f"https://github.com/{gh_org}/{name}.git"}
                else:
                    bb_ws = os.environ.get("BITBUCKET_WORKSPACE", "")
                    if not bb_ws:
                        print(f"[ygs] WARNING: cannot expand '{name}' — BITBUCKET_WORKSPACE not set", file=sys.stderr, flush=True)
                        continue
                    entry = {"url": f"https://bitbucket.org/{bb_ws}/{name}.git"}
                if _force_skills_cli:
                    entry["type"] = "skills-cli"
        normalized.append(entry)
    repos = normalized

    for repo in repos:
        url = repo.get("url", "").strip()
        if not url:
            continue

        # type="skills-cli": delegate to `npx skills add <repo>` (vercel-labs/skills).
        # Installs directly into ~/.claude/skills/ — no symlink step needed.
        # Does NOT support sparse checkout; avoid for large repos.
        if repo.get("type", "") == "skills-cli":
            _install_via_skills_cli(url, skills_base)
            continue

        branch = repo.get("branch", "main")
        skills_dir = repo.get("skills_dir", "")  # empty = auto-detect
        # sparse=True by default — only download skills_dir, not the whole repo
        sparse = bool(repo.get("sparse", True))
        token_env = repo.get("token_env", "")

        # Inject credentials into URL for private repos (never log the substituted URL).
        # Explicit token_env/username_env fields take precedence; otherwise auto-detect
        # from well-known env vars for bitbucket.org and github.com.
        clone_url = url
        token_env = repo.get("token_env", "")
        username_env = repo.get("username_env", "")
        if not token_env:
            if "bitbucket.org" in url:
                token_env, username_env = "BITBUCKET_TOKEN", "BITBUCKET_USERNAME"
            elif "github.com" in url:
                token_env = "GH_TOKEN"
        token = os.environ.get(token_env, "") if token_env else ""
        username = os.environ.get(username_env, "") if username_env else ""
        if token:
            if username:
                # Bitbucket / basic-auth: https://user:token@host/...
                clone_url = url.replace("https://", f"https://{username}:{token}@")
            else:
                # GitHub token: https://token@github.com/...
                clone_url = url.replace("https://", f"https://{token}@")

        slug = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        dest = skills_base / f"_extra_{slug}"
        already_exists = dest.exists() and (dest / ".git").exists()
        if not already_exists:
            _t0 = _time.monotonic()
            if sparse:
                print(f"[ygs] sparse-cloning {url} ({skills_dir} only)...", flush=True)
                try:
                    r = subprocess.run(
                        ["git", "clone", "--depth", "1", "--filter=blob:none",
                         "--sparse", "--branch", branch, clone_url, str(dest)],
                        capture_output=True, text=True, timeout=60,
                    )
                    if r.returncode != 0:
                        print(f"[ygs] WARNING: sparse clone of {url} failed: {r.stderr.strip()}", file=sys.stderr, flush=True)
                        continue
                    r2 = subprocess.run(
                        ["git", "-C", str(dest), "sparse-checkout", "set", skills_dir],
                        capture_output=True, text=True, timeout=60,
                    )
                    if r2.returncode != 0:
                        print(f"[ygs] WARNING: sparse-checkout set failed: {r2.stderr.strip()}", file=sys.stderr, flush=True)
                        continue
                    print(f"[ygs] sparse clone of {url} in {_time.monotonic() - _t0:.0f}s", flush=True)
                except subprocess.TimeoutExpired:
                    print(f"[ygs] WARNING: sparse clone of {url} timed out", file=sys.stderr, flush=True)
                    continue
            else:
                print(f"[ygs] cloning extra skills repo {url}...", flush=True)
                try:
                    clone_repo(url, dest, depth=1, http_token=token, http_username=username or "x-token-auth")
                    print(f"[ygs] cloned {url} in {_time.monotonic() - _t0:.0f}s", flush=True)
                except Exception as exc:
                    print(f"[ygs] WARNING: clone of {url} failed: {exc}", file=sys.stderr, flush=True)
                    continue

        # Resolve skills_dir: explicit value wins; otherwise try common conventions
        if skills_dir:
            src_skills = dest / skills_dir
        else:
            _candidates = [".claude/skills", "skills", ".skills"]
            src_skills = next((dest / c for c in _candidates if (dest / c).is_dir()), None)
            if src_skills is None:
                print(f"[ygs] WARNING: no skills dir found in {dest} (tried {_candidates})", file=sys.stderr, flush=True)
                continue
        if not src_skills.is_dir():
            print(f"[ygs] WARNING: skills_dir '{skills_dir}' not found in {dest}", file=sys.stderr, flush=True)
            continue
        count = 0
        extra_names: list[str] = []
        for skill_dir in sorted(src_skills.iterdir()):
            if skill_dir.is_dir():
                link = skills_base / skill_dir.name
                link.unlink(missing_ok=True)
                link.symlink_to(skill_dir.resolve())
                count += 1
                extra_names.append(skill_dir.name)
        if count:
            print(f"[ygs] {count} extra skills from {slug} installed → {skills_base}/", flush=True)
            safe_slug = slug.upper().replace("-", "_").replace(".", "_")
            print(f"::add-task-context EXTRA_SKILLS_{safe_slug}_COUNT::{count}")
            print(f"::add-task-context EXTRA_SKILLS_{safe_slug}_INSTALLED::{','.join(extra_names)}")
            # Extend global inventory and known-skills set so run_claude() includes extras
            global _SKILLS_INVENTORY, _KNOWN_SKILLS
            _KNOWN_SKILLS.update(extra_names)
            extra_lines = []
            for name in extra_names:
                skill_path = skills_base / name / "SKILL.md"
                try:
                    first_line = skill_path.read_text(errors="replace").split("\n")[0].lstrip("# ").strip()
                except OSError:
                    first_line = ""
                extra_lines.append(f"- `/{name}` — {first_line}")
            if extra_lines and _SKILLS_INVENTORY:
                _SKILLS_INVENTORY = _SKILLS_INVENTORY + "\n" + "\n".join(extra_lines)
            elif extra_lines:
                _SKILLS_INVENTORY = (
                    "## Available Skills\n"
                    "Use the most applicable skill below. If none fits, proceed without one.\n\n"
                    + "\n".join(extra_lines)
                )


@dataclass
class ClaudeResult:
    exit_code: int
    output: str
    status_json: dict = field(default_factory=dict)
    status: str = "UNKNOWN"


def extract_status_json(output: str) -> dict:
    """Extract the last JSON object containing a 'status' key from output.

    Scans lines from the end. Handles JSON embedded mid-line and correctly
    handles } inside string values by using the stdlib JSON parser.
    """
    decoder = json.JSONDecoder()
    for line in reversed(output.splitlines()):
        if '"status"' not in line:
            continue
        for i, ch in enumerate(line):
            if ch == "{":
                try:
                    obj, _ = decoder.raw_decode(line, i)
                    if isinstance(obj, dict) and "status" in obj:
                        return obj
                except json.JSONDecodeError:
                    pass
    return {}


# Task-specific system prompts: short, role-focused, token-efficient.
# Keeps Bedrock cross-region input under the size limit while giving Claude
# the right mental model for each task type. Pass as system_prompt= to run_claude().
# Token-efficiency rules appended to every system prompt.
_TOKEN_EFFICIENCY_RULES = (
    " EFFICIENCY RULES (follow strictly to minimize tokens and turns): "
    "1) Batch tool calls — invoke multiple independent tools in a single turn whenever possible. "
    "2) Never re-read a file you just read; never repeat information already in context. "
    "3) Produce NO intermediate prose between tool calls — think silently, act immediately. "
    "4) Prefer Read over Bash for file inspection. "
    "5) When you have enough information to write the output, do it in one turn — don't ask clarifying questions. "
    "6) Produce the final JSON status line immediately after writing all output files. "
    "7) Aim to complete the entire task in ≤15 turns."
)

SYSTEM_PROMPTS = {
    # Writing / modifying code in a repo
    "implement": (
        "You are an ultra-concise, high-performance technical architect and expert programmer. "
        "Execute instructions exactly. Match existing language, style, and patterns — never rewrite in a different language. "
        "Edit existing files; create new ones only when required. No gold-plating, no extra abstractions. "
        "Output the final JSON status line when done."
        + _TOKEN_EFFICIENCY_RULES
    ),
    # Planning / analysis only — no file writes
    "plan": (
        "You are an ultra-concise technical architect. "
        "Analyse the task and produce a minimal, actionable implementation plan. "
        "No code. No file edits. Output structured text then the final JSON status line."
        + _TOKEN_EFFICIENCY_RULES
    ),
    # PR / code review — read-only analysis
    "review": (
        "You are an ultra-concise, high-performance security and correctness code reviewer. "
        "Fetch the PR diff, identify real defects only — no style nitpicks. "
        "Rank by severity (CRITICAL > HIGH > MEDIUM > LOW). Be terse: one line per finding. "
        "Write findings.json then output the final JSON status line."
        + _TOKEN_EFFICIENCY_RULES
    ),
    # Standup / risk-scan / PR-queue — tracker data synthesis
    "standup": (
        "You are an ultra-concise engineering lead producing a daily standup brief. "
        "Use only the data provided. Never invent issues, PRs, or team members. "
        "Output Slack-safe mrkdwn. End with the final JSON status line."
        + _TOKEN_EFFICIENCY_RULES
    ),
    # Responding to PR comments / applying feedback
    "respond": (
        "You are an ultra-concise, high-performance expert programmer. "
        "Address each review comment with a minimal, targeted code change. "
        "Follow existing conventions exactly. No unrequested refactors. "
        "Output the final JSON status line when done."
        + _TOKEN_EFFICIENCY_RULES
    ),
    # Learning / summarisation tasks
    "learn": (
        "You are an ultra-concise technical writer. "
        "Extract the key lessons from the provided context and write them in bullet form. "
        "Be specific — no generic advice. Output the final JSON status line."
        + _TOKEN_EFFICIENCY_RULES
    ),
    # General Q&A / free-form assistant — used by ygs-ask and unknown-intent fallback
    "adhoc": (
        "You are a concise, expert assistant. Answer questions directly using available tools. "
        "Fetch live data from Jira or GitHub via Bash when the question involves team or project data. "
        "Format ALL output as Slack mrkdwn: *bold*, bullet points, `code`, and links. "
        "Keep answers concise — Slack threads, not essays. "
        "Output the final JSON status line when done."
        + _TOKEN_EFFICIENCY_RULES
    ),
}

# Default: safe for any task
_DEFAULT_SYSTEM_PROMPT = (
    "You are an ultra-concise, high-performance technical architect and expert programmer. "
    "Execute instructions exactly. Match existing language, style, and patterns. "
    "No gold-plating. Output the final JSON status line when done."
    + _TOKEN_EFFICIENCY_RULES
)


def run_claude(
    prompt: str,
    working_dir: Path,
    model: str | None = None,
    max_turns: int = 30,
    log_file: Path | None = None,
    extra_env: dict | None = None,
    allowed_tools: str | None = "Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS",
    system_prompt: str | None = None,
    process_timeout: int | None = None,
) -> ClaudeResult:
    """Run claude CLI, return structured result.

    The prompt is passed via stdin to avoid ARG_MAX limits.
    system_prompt: use one of the SYSTEM_PROMPTS presets or a custom string.
                   Defaults to _DEFAULT_SYSTEM_PROMPT (short, Bedrock-safe).
    """
    _ensure_ygs_skills()

    sp = system_prompt or _DEFAULT_SYSTEM_PROMPT
    if _SKILLS_INVENTORY:
        sp = sp + "\n\n" + _SKILLS_INVENTORY
    cmd = [
        "claude", "--print", "--dangerously-skip-permissions",
        "--system-prompt", sp,
    ]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if model:
        cmd += ["--model", model]
    cmd += ["--max-turns", str(max_turns)]
    # Prompt passed via stdin to avoid ARG_MAX limits on large prompts

    # Pass only vars that claude itself needs — avoids injecting the entire
    # formicary job environment (50+ vars including multi-line SSH keys) into
    # the Bedrock system prompt, which has a strict size limit.
    _CLAUDE_VARS = {
        "HOME", "PATH", "USER", "SHELL", "TERM", "LANG", "LC_ALL",
        "TMPDIR", "TMP", "TEMP",
        # Bedrock / API auth
        "CLAUDE_CODE_USE_BEDROCK",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "ANTHROPIC_API_KEY",
        # Model selection
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        # AWS SDK (needed when bedrock calls go through aws-sdk)
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        # Tracker credentials — needed by you-got-skills that call APIs via Bash
        "GH_TOKEN", "GITHUB_TOKEN",
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_HOST", "JIRA_API_TOKEN", "JIRA_AUTH",
        "JIRA_PROJECT",
        "BITBUCKET_WORKSPACE", "BITBUCKET_REPO", "BITBUCKET_TOKEN", "BITBUCKET_USERNAME",
        # Slack — needed by skills that read standup channel signals
        "SLACK_BOT_TOKEN", "SLACK_CHANNEL",
        # Project context
        "GH_ORG", "GH_REPO",
        "STANDUP_TEAM_MEMBERS", "STANDUP_LOOKBACK_HOURS",
        # Workspace
        "WORKSPACE_DIR",
    }
    env = {k: v for k, v in os.environ.items() if k in _CLAUDE_VARS}
    if extra_env:
        env.update(extra_env)

    # Save prompt to log dir for debugging
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.with_suffix(".prompt.txt").write_text(prompt)
        except OSError as e:
            print(f"[claude] WARNING: could not write prompt log {log_file}: {e}", file=sys.stderr, flush=True)

    # Resolve process timeout: explicit param > env var > None (no limit)
    _ptimeout = process_timeout
    if _ptimeout is None:
        _env_timeout = os.environ.get("MAX_CLAUDE_PROCESS_TIMEOUT", "").strip()
        if _env_timeout:
            try:
                _ptimeout = int(_env_timeout)
            except ValueError:
                pass

    output_lines: list[str] = []
    stderr_lines: list[str] = []

    print(f"[claude] starting: model={model or 'default'} max_turns={max_turns} timeout={_ptimeout}s cwd={working_dir} prompt_chars={len(prompt)}", flush=True)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=working_dir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            # Run in a new session so killpg() kills the entire process tree
            # (Claude spawns child processes; killing only the parent leaves
            # grandchildren holding the stdout pipe open indefinitely).
            start_new_session=True,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        assert proc.stdin is not None

        def _drain_stderr() -> None:
            for line in proc.stderr:
                stderr_lines.append(line)
                sys.stderr.write(line)
                sys.stderr.flush()

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        # Drain stdout in a background thread so the main thread can enforce the
        # wall-clock deadline without blocking on a line-iterator that may never EOF.
        # This is the core of the timeout safety mechanism: even if grandchild
        # processes survive killpg() and keep the pipe open, the main thread will
        # unblock after _ptimeout + _DRAIN_GRACE_SECONDS by closing stdout forcibly.
        _DRAIN_GRACE_SECONDS = 10  # extra time after kill for the drain to finish

        _stdout_done = threading.Event()

        def _drain_stdout() -> None:
            try:
                for line in proc.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    output_lines.append(line)
            finally:
                _stdout_done.set()

        stdout_thread = threading.Thread(target=_drain_stdout, daemon=True)
        stdout_thread.start()

        # Kill entire process group after process_timeout seconds.
        # proc.kill() alone is insufficient — Claude spawns child processes that
        # keep the stdout pipe open if the parent dies first.
        # After killing the group we close stdout so _drain_stdout() unblocks.
        _kill_timer: threading.Timer | None = None
        if _ptimeout:
            def _kill_on_timeout() -> None:
                print(f"[claude] ERROR: process exceeded {_ptimeout}s — killing process group", file=sys.stderr, flush=True)
                try:
                    import signal as _signal
                    os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                except OSError:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                # Close stdout so the drain thread unblocks even if grandchildren survive
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            _kill_timer = threading.Timer(_ptimeout, _kill_on_timeout)
            _kill_timer.daemon = True
            _kill_timer.start()

        proc.stdin.write(prompt)
        proc.stdin.close()

        # Wait for drain to complete, with a hard deadline of timeout + grace
        _drain_deadline = (_ptimeout + _DRAIN_GRACE_SECONDS) if _ptimeout else None
        _stdout_done.wait(timeout=_drain_deadline)
        if not _stdout_done.is_set():
            # Still blocked — force-close the pipe and let the daemon thread die
            sys.stderr.write("[claude] WARNING: stdout drain timed out — closing pipe\n")
            sys.stderr.flush()
            try:
                proc.stdout.close()
            except OSError:
                pass
        stdout_thread.join(timeout=5)

        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            sys.stderr.write("[claude] WARNING: proc.wait() timed out — process may be a zombie\n")
            sys.stderr.flush()
        if _kill_timer is not None:
            _kill_timer.cancel()
        stderr_thread.join(timeout=5)
        if stderr_thread.is_alive():
            # The child process leaked a grandchild that still holds the stderr
            # pipe open. The drain thread is a daemon and will be reaped at
            # interpreter exit, but we warn so the log makes the truncation visible.
            sys.stderr.write("[claude] WARNING: stderr drain timed out — stderr output may be incomplete\n")
            sys.stderr.flush()
        exit_code = proc.returncode
    except FileNotFoundError:
        print("ERROR: 'claude' CLI not found. Install @anthropic-ai/claude-code.", file=sys.stderr)
        return ClaudeResult(exit_code=1, output="claude not found", status="ERROR")

    full_output = "".join(output_lines)
    full_stderr = "".join(stderr_lines)

    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(full_output)
            if full_stderr:
                log_file.with_suffix(".stderr.log").write_text(full_stderr)
        except OSError as e:
            print(f"[claude] WARNING: could not write log file {log_file}: {e}", file=sys.stderr, flush=True)

    if _KNOWN_SKILLS:
        _hits = [s for s in re.findall(r'/([a-z][a-z0-9-]+)', full_output) if s in _KNOWN_SKILLS]
        print(f"::add-task-context SKILLS_INVOKED::{','.join(dict.fromkeys(_hits)) or 'none'}")

    if exit_code != 0:
        # "Reached max turns" is a normal operating condition, not a hard error.
        # Claude may have made partial progress — commit/push that work rather than
        # discarding it.  Any other non-zero exit is a genuine failure.
        if "Reached max turns" in full_output or "Reached max turns" in full_stderr:
            print(f"[claude] max turns ({max_turns}) reached — treating as partial result", flush=True)
            status_json = extract_status_json(full_output) or {}
            status_json.setdefault("status", "MAX_TURNS_REACHED")
            return ClaudeResult(
                exit_code=exit_code,
                output=full_output,
                status_json=status_json,
                status="MAX_TURNS_REACHED",
            )
        # If Claude produced a valid JSON status line (DONE or ERROR), treat it as a
        # soft completion even if the process exited non-zero. The claude CLI sometimes
        # exits 1 when the task status is ERROR — the skill handler will post the reason
        # to Slack. Raising RuntimeError here would wrap the reason in an ugly error
        # message and confuse downstream callers.
        soft_status = extract_status_json(full_output)
        if soft_status.get("status") in ("DONE", "ERROR", "PARTIAL"):
            print(
                f"[claude] non-zero exit ({exit_code}) but valid status={soft_status['status']} found — "
                f"treating as soft completion",
                flush=True,
            )
            return ClaudeResult(
                exit_code=0,
                output=full_output,
                status_json=soft_status,
                status=soft_status["status"],
            )
        stderr_hint = f"\nStderr:\n{full_stderr[-1000:]}" if full_stderr.strip() else ""
        raise RuntimeError(
            f"claude exited with code {exit_code}.{stderr_hint}\n"
            f"Last stdout:\n{full_output[-2000:] if len(full_output) > 2000 else full_output}"
        )

    status_json = extract_status_json(full_output)
    status = status_json.get("status", "UNKNOWN")

    return ClaudeResult(
        exit_code=exit_code,
        output=full_output,
        status_json=status_json,
        status=status,
    )
