"""Invoke Claude Code CLI and parse structured output.

Wraps the `claude` CLI with proper flags, captures output,
and extracts the JSON status line from the response.
"""

import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path


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


def run_claude(
    prompt: str,
    working_dir: Path,
    model: str | None = None,
    max_turns: int = 30,
    log_file: Path | None = None,
    extra_env: dict | None = None,
) -> ClaudeResult:
    """Run claude CLI, return structured result.

    The prompt is passed as a command-line argument to avoid shell injection.
    Output is streamed to stdout (for live monitoring) and captured.
    """
    # Restrict tools + use a short system prompt to stay under Bedrock's input-size limit.
    # Claude Code's default system prompt is very large and causes "Prompt is too long"
    # against Bedrock cross-region inference endpoints.
    # The replacement must explicitly include "follow existing conventions" — without it
    # Claude will rewrite files in a different language or style.
    _TOOLS = "Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS"
    _SYSTEM_PROMPT = (
        "You are an expert software engineer. "
        "Follow the instructions in the user prompt exactly. "
        "Always follow the existing code conventions, language, style, and patterns of the repository. "
        "Never rewrite or replace existing code in a different language. "
        "Prefer editing existing files over creating new ones. "
        "Do not add unnecessary abstractions or features beyond what is asked."
    )
    cmd = [
        "claude", "--print", "--dangerously-skip-permissions",
        "--system-prompt", _SYSTEM_PROMPT,
        "--allowedTools", _TOOLS,
    ]
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
    }
    env = {k: v for k, v in os.environ.items() if k in _CLAUDE_VARS}
    if extra_env:
        env.update(extra_env)

    # Save prompt to log dir for debugging
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.with_suffix(".prompt.txt").write_text(prompt)

    output_lines: list[str] = []
    stderr_lines: list[str] = []

    print(f"[claude] starting: model={model or 'default'} max_turns={max_turns} cwd={working_dir} prompt_chars={len(prompt)}", flush=True)

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

        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            output_lines.append(line)
        proc.wait()
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
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(full_output)
        if full_stderr:
            log_file.with_suffix(".stderr.log").write_text(full_stderr)

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
