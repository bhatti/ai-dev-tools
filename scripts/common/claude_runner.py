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
    cmd = ["claude", "--print", "--dangerously-skip-permissions"]
    if model:
        cmd += ["--model", model]
    cmd += ["--max-turns", str(max_turns)]
    # Prompt passed via stdin to avoid ARG_MAX limits on large prompts

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    output_lines: list[str] = []
    stderr_lines: list[str] = []

    print(f"[claude] starting: model={model or 'default'} max_turns={max_turns} cwd={working_dir}", flush=True)

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
