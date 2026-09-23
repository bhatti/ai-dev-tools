"""Shared subprocess helper for scripts that invoke CLI tools (gh, git, etc.)."""

import subprocess


def run_cmd(
    cmd: list[str],
    check: bool = True,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a command, capturing stdout and stderr.

    Raises CalledProcessError with stderr populated when check=True and the
    command exits non-zero. Falls back to stdout content when stderr is empty
    so the caller always gets a useful diagnostic in the exception.

    Args:
        cmd: Command and arguments to run.
        check: Raise on non-zero exit when True (default).
        cwd: Working directory for the subprocess; None uses the caller's cwd.
    """
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if check and result.returncode != 0:
        stderr = result.stderr or result.stdout
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=stderr)
    return result
