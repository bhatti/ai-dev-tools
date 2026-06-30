"""Git operations: clone, branch, commit, push.

All operations are idempotent — safe to call multiple times.
"""

import os
import re
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True, env=None) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, check=False, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    return result


def _slug(text: str, max_len: int = 40) -> str:
    """Convert arbitrary text to a URL-safe slug."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-")


def make_branch_name(issue_id: str, title: str, nonce: str | None = None) -> str:
    """Generate branch name: ai/{issue_id}-{slug}-{nonce}"""
    slug = _slug(title)
    if nonce is None:
        import secrets
        nonce = secrets.token_hex(4)
    return f"ai/{issue_id}-{slug}-{nonce}"


def _embed_credentials(url: str, username: str, token: str) -> str:
    """Return URL with credentials embedded: https://user:token@host/path.

    Username is percent-encoded; token is left as-is (Atlassian ATATT tokens
    contain = signs that must not be encoded for git to accept them).
    Raises ValueError if url has no '://' scheme separator.
    """
    from urllib.parse import quote

    scheme_end = url.index("://")  # ValueError if no scheme — callers must pass https:// URLs
    scheme = url[:scheme_end]
    rest = url[scheme_end + 3:]
    return f"{scheme}://{quote(username, safe='')}:{token}@{rest}"


def _redact_url(url: str) -> str:
    """Replace credentials in a URL with *** for safe logging."""
    return re.sub(r"://[^@]*@", "://***@", url)


def clone_repo(
    url: str,
    dest: Path,
    depth: int = 100,
    ssh_key: str = "",
    http_token: str = "",
    http_username: str = "x-token-auth",
) -> Path:
    """Clone repo. If dest exists and is a git repo, fetch instead of re-cloning.

    Auth priority:
      1. http_token: credentials embedded in URL (works with Atlassian ATATT tokens)
      2. ssh_key: writes key to temp file, uses GIT_SSH_COMMAND
      3. Neither: relies on ssh-agent or ~/.ssh/id_* in the container
    """
    import shutil
    import tempfile

    dest = Path(dest)
    key_path = ""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    if http_token:
        git_url = _embed_credentials(url, http_username, http_token)
        # No GIT_SSH_COMMAND — pure HTTPS path
    elif ssh_key:
        git_url = url
        fd, key_path = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(fd, "w") as f:
            f.write(ssh_key)
            if not ssh_key.endswith("\n"):
                f.write("\n")
        os.chmod(key_path, 0o600)
        env["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o StrictHostKeyChecking=no -o BatchMode=yes"
    else:
        git_url = url
        env["GIT_SSH_COMMAND"] = "ssh -o StrictHostKeyChecking=no -o BatchMode=yes"

    safe_url = _redact_url(git_url)

    try:
        if dest.exists() and (dest / ".git").exists():
            try:
                # Refresh the stored remote URL so a rotated token is always current
                if http_token:
                    _run(["git", "remote", "set-url", "origin", git_url], cwd=dest, env=env)
                _run(["git", "fetch", "--depth", str(depth), "origin"], cwd=dest, env=env)
                return dest
            except subprocess.CalledProcessError as e:
                print(f"git fetch failed, re-cloning ({e.returncode}): {e.stderr.strip()}", file=sys.stderr)
                shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", "--depth", str(depth), git_url, str(dest)],
            capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            print(f"git clone failed: {result.stderr.strip()}", file=sys.stderr)
            # Raise with redacted command so the token is never in the exception message
            raise subprocess.CalledProcessError(
                result.returncode,
                ["git", "clone", "--depth", str(depth), safe_url, str(dest)],
                result.stdout,
                result.stderr,
            )
    finally:
        if key_path and os.path.exists(key_path):
            os.unlink(key_path)

    return dest


def configure_git(repo_path: Path, name: str, email: str) -> None:
    """Set local git user identity."""
    _run(["git", "config", "user.name", name], cwd=repo_path)
    _run(["git", "config", "user.email", email], cwd=repo_path)


def create_branch(repo_path: Path, branch_name: str) -> str:
    """Create/checkout branch. Idempotent — reuses if it already exists."""
    result = _run(["git", "branch", "--list", branch_name], cwd=repo_path)
    if result.stdout.strip():
        _run(["git", "checkout", branch_name], cwd=repo_path)
    else:
        # Check if branch exists on remote
        remote_result = _run(
            ["git", "ls-remote", "--heads", "origin", branch_name], cwd=repo_path, check=False
        )
        if remote_result.stdout.strip():
            _run(["git", "checkout", "-b", branch_name, f"origin/{branch_name}"], cwd=repo_path)
        else:
            _run(["git", "checkout", "-b", branch_name], cwd=repo_path)
    return branch_name


def current_branch(repo_path: Path) -> str:
    """Return the current branch name."""
    result = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    return result.stdout.strip()


def commit_all(repo_path: Path, message: str) -> bool:
    """Stage all changes and commit. Returns False if nothing to commit."""
    _run(["git", "add", "-A"], cwd=repo_path)
    result = _run(["git", "status", "--porcelain"], cwd=repo_path)
    if not result.stdout.strip():
        return False
    _run(["git", "commit", "-m", message], cwd=repo_path)
    return True


def push_branch(
    repo_path: Path,
    branch: str,
    force_with_lease: bool = True,
    http_token: str = "",
    http_username: str = "x-token-auth",
    url: str = "",
) -> None:
    """Push branch to origin.

    http_token/http_username/url: when provided, refreshes the stored remote URL
    before pushing so a rotated token is always current.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if http_token and url:
        git_url = _embed_credentials(url, http_username, http_token)
        _run(["git", "remote", "set-url", "origin", git_url], cwd=repo_path, env=env)

    cmd = ["git", "push", "origin", branch]
    if force_with_lease:
        cmd.append("--force-with-lease")
    result = _run(cmd, cwd=repo_path, check=False, env=env)
    if result.returncode != 0:
        stderr = result.stderr + result.stdout
        # Only fall back to set-upstream on first push (no tracking branch yet)
        if "has no upstream branch" in stderr or "no upstream configured" in stderr:
            _run(["git", "push", "--set-upstream", "origin", branch], cwd=repo_path, env=env)
        else:
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)


def get_commit_count(repo_path: Path, base_branch: str = "main") -> int:
    """Count commits on current branch since base_branch."""
    result = _run(
        ["git", "rev-list", "--count", f"{base_branch}..HEAD"], cwd=repo_path, check=False
    )
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def detect_repo_url(org: str, repo: str, use_ssh: bool = True) -> str:
    """Build the git clone URL."""
    if use_ssh:
        return f"git@github.com:{org}/{repo}.git"
    return f"https://github.com/{org}/{repo}.git"


def detect_bitbucket_url(workspace: str, repo: str, use_ssh: bool = True) -> str:
    """Build the BitBucket clone URL."""
    if use_ssh:
        return f"git@bitbucket.org:{workspace}/{repo}.git"
    return f"https://bitbucket.org/{workspace}/{repo}.git"
