"""Run a YGS skill against a repo with flag parsing, repo cloning, and report generation.

Reads RAW_ARGS env var (set by formicary from Slack router), parses it into
skill name + flags + instructions, resolves repo/branch/tracker, clones the
repo, loads and invokes the skill via Claude, and posts results to Slack.

Usage (inside formicary pod):
    RAW_ARGS="ygs-analyze --repo myapp --branch dev -- focus on test coverage"
    python -m scripts.skill.run_skill

Writes: /workspace/skill_result.json
        /workspace/reports/report.{md,html}
        /workspace/logs/skill.log

Exit codes: 0=done, 1=error
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from scripts.common.claude_runner import (
    ensure_ygs_skills,
    run_claude,
)
from scripts.common.config import (
    MODEL_SHORTNAMES,
    get_workspace_dir,
    load_config,
    validate_claude_config,
)
from scripts.common.git_utils import clone_repo, create_branch, resolve_clone_auth
from scripts.skill.flags import SkillFlags, parse_skill_flags, resolve_repo, resolve_tracker
from scripts.common.slack_format import format_for_slack
from scripts.standup.slack_client import notify as slack_notify

# Reuse shared helpers from adhoc run_skill — these are stable, well-tested utilities.
from scripts.adhoc.run_skill import (
    _build_prompt,
    _load_skill_md,
    _setup_environment,
    _strip_for_slack,
    _system_prompt_for_skill,
)


def _clone_target_repo(
    clone_url: str, branch: str, config: dict, tracker: str, workspace: Path,
) -> Path | None:
    """Clone the target repo into /workspace/repo and checkout branch.

    Uses resolve_clone_auth() for credential extraction (shared with clone_by_tracker).
    Returns the repo path on success, None on failure.
    """
    dest = workspace / "repo"
    http_token, http_username, ssh_key = resolve_clone_auth(config, tracker)

    try:
        if http_token:
            clone_repo(clone_url, dest, depth=100, http_token=http_token, http_username=http_username)
        elif ssh_key:
            clone_repo(clone_url, dest, depth=100, ssh_key=ssh_key)
        else:
            clone_repo(clone_url, dest, depth=100)
        print(f"[skill] cloned {clone_url} -> {dest}", flush=True)
    except Exception as e:
        print(f"[skill] clone failed: {e}", file=sys.stderr, flush=True)
        return None

    try:
        actual = create_branch(dest, branch)
        print(f"[skill] checked out branch: {actual}", flush=True)
    except Exception as e:
        print(f"[skill] branch checkout failed ({branch}): {e}", file=sys.stderr, flush=True)

    return dest


def _resolve_model(config: dict, flags: SkillFlags) -> str | None:
    """Resolve model from flags, AI_MODEL_OVERRIDE env, or AI_MODEL config."""
    model = config.get("AI_MODEL")

    override = flags.model or os.getenv("AI_MODEL_OVERRIDE", "").strip()
    if override and override not in ("<no value>", "{{.AiModel}}"):
        shortnames = {
            "haiku": config.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", MODEL_SHORTNAMES["haiku"]),
            "sonnet": config.get("ANTHROPIC_DEFAULT_SONNET_MODEL", MODEL_SHORTNAMES["sonnet"]),
            "opus": config.get("ANTHROPIC_DEFAULT_OPUS_MODEL", MODEL_SHORTNAMES["opus"]),
            **{k: v for k, v in MODEL_SHORTNAMES.items() if k not in ("haiku", "sonnet", "opus")},
        }
        model = shortnames.get(override.lower(), override)

    return model


def _write_result(workspace: Path, data: dict) -> None:
    p = workspace / "skill_result.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _append_model_footer(text: str, model: str | None) -> str:
    """Append a model footer line to report text."""
    if model:
        return f"{text.rstrip()}\n\n---\n*Model: {model}*\n"
    return text


def _write_reports(workspace: Path, skill: str, output_text: str, status_data: dict,
                   model: str | None = None) -> None:
    """Write reports/report.md, reports/report.html, reports/result.json."""
    try:
        from scripts.common.report_renderer import render_simple_html

        md_with_model = _append_model_footer(output_text, model)
        reports_dir = workspace / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "report.md").write_text(md_with_model, encoding="utf-8")
        (reports_dir / "report.html").write_text(
            render_simple_html(skill, md_with_model), encoding="utf-8"
        )
        (reports_dir / "result.json").write_text(
            json.dumps(status_data, indent=2), encoding="utf-8"
        )
        print("[skill] wrote reports/report.md, report.html, result.json", flush=True)
    except Exception as e:
        print(f"[skill] WARNING: could not write reports/: {e}", flush=True)



def _find_report_content(workspace: Path) -> str | None:
    """Check for report files written by Claude. Returns content or None."""
    candidates = [
        "standup_brief.md",
        "risk_report.md",
        "adhoc_report.md",
        "pr_queue_report.md",
        "reports/report.md",
    ]
    for candidate in candidates:
        path = workspace / candidate
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                print(f"[skill] using report from {candidate} ({len(content)} chars)", flush=True)
                return content
    return None


def main() -> None:
    config = load_config()
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    logs_dir = workspace / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Parse flags from RAW_ARGS env var.
    raw_args = os.getenv("RAW_ARGS", "").strip()
    # Fallback: if RAW_ARGS is empty, try SKILL_PROMPT (for backward compat with adhoc routing).
    if not raw_args:
        skill_name = os.getenv("SKILL_NAME", "").strip()
        skill_prompt = os.getenv("SKILL_PROMPT", "").strip()
        if skill_name and skill_prompt:
            raw_args = f"{skill_name} -- {skill_prompt}"
        elif skill_name:
            raw_args = skill_name

    flags = parse_skill_flags(raw_args)
    if not flags.skill:
        print("[skill] ERROR: no skill name in RAW_ARGS", file=sys.stderr, flush=True)
        _write_result(workspace, {"status": "ERROR", "reason": "no skill name provided"})
        sys.exit(1)

    # Resolve tracker, repo, branch.
    tracker = resolve_tracker(flags, config)
    clone_url, branch = resolve_repo(flags, config, tracker)

    # Override DEFAULT_TRACKER in config so downstream code sees the resolved tracker.
    config["DEFAULT_TRACKER"] = tracker
    os.environ["DEFAULT_TRACKER"] = tracker

    print(f"[skill] skill={flags.skill} tracker={tracker} repo={clone_url or '(none)'} "
          f"branch={branch} instructions={flags.instructions[:80]}...", flush=True)

    # Set up environment: .ygs/tracker.yml, JIRA_AUTH, gh auth.
    sprint_team = _setup_environment(config)

    # Clone target repo if we have a URL.
    codebase_dir = ""
    if clone_url:
        repo_path = _clone_target_repo(clone_url, branch, config, tracker, workspace)
        if repo_path:
            codebase_dir = str(repo_path)
            os.environ["CODEBASE_DIR"] = codebase_dir
            config["CODEBASE_DIR"] = codebase_dir

    # Install skills (YGS + EXTRA_SKILLS_REPOS + project overrides from CODEBASE_DIR).
    ensure_ygs_skills()

    # Load the requested skill.
    skill_md = _load_skill_md(flags.skill)
    if skill_md:
        print(f"[skill] loaded SKILL.md for {flags.skill} ({len(skill_md)} chars)", flush=True)
    else:
        print(f"[skill] no SKILL.md found for {flags.skill} — using fallback prompt", flush=True)

    # Emit task context markers.
    model_val = config.get("AI_MODEL") or ""
    print(f"::add-task-context SELECTED_TRACKER::{tracker}", flush=True)
    print(f"::add-task-context SKILL::{flags.skill}", flush=True)
    print(f"::add-task-context SKILL_LOADED::{'yes' if skill_md else 'no'}", flush=True)
    print(f"::add-task-context SELECTED_MODEL::{model_val}", flush=True)
    if clone_url:
        print(f"::add-task-context REPO_URL::{clone_url}", flush=True)
        print(f"::add-task-context BRANCH::{branch}", flush=True)

    # Service awareness: if a service sidecar is running, inject info into instructions.
    service_image = os.getenv("SERVICE_IMAGE", "").strip()
    service_name = os.getenv("SERVICE_NAME", "skill-service").strip()
    service_port = os.getenv("SERVICE_PORT", "9000").strip()
    service_command = os.getenv("SERVICE_COMMAND", "").strip()
    service_args = os.getenv("SERVICE_ARGS", "").strip()
    service_info = ""
    if service_image and service_image not in ("<no value>", "{{.ServiceImage}}"):
        cmd_desc = ""
        if service_command or service_args:
            full_cmd = f"{service_command} {service_args}".strip()
            cmd_desc = f"\n- Command: `{full_cmd}`"
        service_info = (
            f"\n\n## Background Service\n\n"
            f"A background service is running alongside this task:\n"
            f"- Image: {service_image}\n"
            f"- Hostname: localhost (or {service_name})\n"
            f"- Port: {service_port}\n"
            f"- Access via: http://localhost:{service_port}"
            f"{cmd_desc}\n"
            f"\nEnv var SERVICE_PORT is set to {service_port}; use it for health checks.\n"
        )
        print(f"[skill] service running: {service_image} at localhost:{service_port}", flush=True)
    elif flags.service:
        print(
            f"[skill] WARNING: --service flag parsed ({flags.service}) but SERVICE_IMAGE env is not set. "
            f"Service must be specified at job submission time via ServiceImage param.",
            flush=True,
        )

    if flags.identifier:
        print(f"[skill] identifier={flags.identifier}", flush=True)
        print(f"::add-task-context IDENTIFIER::{flags.identifier}", flush=True)

    # Build the prompt.
    extra_instructions = flags.instructions
    if flags.identifier:
        extra_instructions = f"Identifier: {flags.identifier}\n{extra_instructions}".strip()
    if service_info:
        extra_instructions = service_info + "\n" + extra_instructions
    if codebase_dir:
        extra_instructions = (
            f"\n## Target Repository\n\n"
            f"The codebase has been cloned to: {codebase_dir}\n"
            f"Branch: {branch}\n"
            f"You can read/analyze files there.\n\n"
            + extra_instructions
        )

    full_prompt = _build_prompt(
        flags.skill, extra_instructions or f"Run the {flags.skill} skill.",
        skill_md, sprint_team=sprint_team or None,
    )

    # Validate Claude credentials.
    validate_claude_config(config)

    # Resolve model and turns.
    model = _resolve_model(config, flags)
    default_turns = int(config.get("MAX_TURNS_ADHOC", config.get("MAX_TURNS_IMPLEMENT", "100")))
    if flags.turns:
        try:
            max_turns = int(flags.turns)
        except ValueError:
            print(f"[skill] WARNING: invalid --turns value {flags.turns!r}, using default {default_turns}", flush=True)
            max_turns = default_turns
    else:
        max_turns = default_turns
    log_path = logs_dir / "skill.log"

    # Invoke Claude.
    try:
        result = run_claude(
            full_prompt,
            working_dir=workspace,
            model=model,
            max_turns=max_turns,
            log_file=log_path,
            allowed_tools="Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS,Skill",
            system_prompt=_system_prompt_for_skill(flags.skill, default="skill"),
            primary_skill=flags.skill,
        )
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr, flush=True)
        _write_result(workspace, {"status": "ERROR", "reason": str(e)})
        try:
            slack_notify(config, f"⚠️ Skill `/{flags.skill}` failed: {str(e)[:500]}")
        except Exception:
            pass
        sys.exit(1)

    # Process output.
    status_data = result.status_json or {"status": result.status}
    _write_result(workspace, status_data)

    output_text = result.output.strip()

    # Check for report files written by Claude.
    report_content = _find_report_content(workspace)
    if report_content:
        output_text = report_content

    # Write reports.
    if output_text and output_text != json.dumps(status_data):
        _write_reports(workspace, flags.skill, output_text, status_data, model=model)

    # Slack posting is handled by the post task (scripts.skill.post) which runs
    # after this task completes and has access to the report artifacts.

    # Use result.status (authoritative runner status) not status_data which is
    # Claude's JSON output and may not reflect MAX_TURNS_REACHED correctly.
    final_status = result.status
    print(f"[skill] status={final_status}", flush=True)
    print(f"::add-task-context SELECTED_MODEL::{model or ''}", flush=True)

    # Treat MAX_TURNS_REACHED as a job failure so Formicary marks it failed
    # and triggers notify-error (the on_failed task).
    if final_status in ("MAX_TURNS_REACHED", "ERROR"):
        print(f"[skill] FAILURE: {final_status} — marking job failed", file=sys.stderr, flush=True)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
