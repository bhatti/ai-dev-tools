"""drop_stored_credentials: the token must not stay readable inside the checkout (real git, local repositories only)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.common.env_scrub import scrubbed_environ
from scripts.common.git_utils import drop_stored_credentials

SECRET = "ATATT-secret-value-123"


def git(repo: Path, *args: str, env: dict | None = None, input: str | None = None) -> subprocess.CompletedProcess:
    isolated = {**(env if env is not None else os.environ), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=repo, env=isolated, capture_output=True, text=True, input=input)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", ".")
    git(r, "remote", "add", "origin", f"https://x-token-auth:{SECRET}@bitbucket.org/ws/repo.git")
    return r


def everything_under_git(repo: Path) -> str:
    return "".join(p.read_text(errors="ignore") for p in (repo / ".git").rglob("*") if p.is_file() and p.stat().st_size < 1_000_000)


def run_helper(repo: Path, host: str, env: dict) -> str:
    """Run OUR repo-local credential helper exactly as git would ("<helper> get", request on stdin) with a controlled
    environment. Deliberately not `git credential fill`: that would also consult the machine's real credential stores."""
    found = git(repo, "config", "--local", "--get", f"credential.https://{host}.helper")
    if found.returncode != 0:
        return ""
    script = found.stdout.strip().removeprefix("!")
    return subprocess.run(["sh", "-c", f"{script} get"], input=f"protocol=https\nhost={host}\n\n", env=env, capture_output=True, text=True).stdout


def test_token_is_gone_from_origin_url_and_everything_else_under_dot_git(repo):
    assert SECRET in everything_under_git(repo)  # precondition: this is what clone_repo leaves behind
    drop_stored_credentials(repo, "https://bitbucket.org/ws/repo.git", "x-token-auth")
    assert SECRET not in everything_under_git(repo)
    assert git(repo, "remote", "get-url", "origin").stdout.strip() == "https://bitbucket.org/ws/repo.git"


def test_the_process_that_runs_git_with_the_token_in_its_environment_still_authenticates(repo):
    drop_stored_credentials(repo, "https://bitbucket.org/ws/repo.git", "x-token-auth")
    out = run_helper(repo, "bitbucket.org", {"PATH": os.environ["PATH"], "BITBUCKET_TOKEN": "tok-from-env"})
    assert "username=x-token-auth" in out and "password=tok-from-env" in out


def test_code_run_with_scrubbed_credentials_gets_nothing(repo, monkeypatch):
    drop_stored_credentials(repo, "https://bitbucket.org/ws/repo.git", "x-token-auth")
    monkeypatch.setenv("BITBUCKET_TOKEN", "tok-from-env")
    monkeypatch.setenv("BITBUCKET_APP_PASSWORD", "app-pw")
    out = run_helper(repo, "bitbucket.org", scrubbed_environ())
    assert "tok-from-env" not in out and "app-pw" not in out and "password=\n" in out  # answers, but with an empty password


def test_the_helper_only_answers_for_the_repositorys_own_host(repo):
    drop_stored_credentials(repo, "https://bitbucket.org/ws/repo.git", "x-token-auth")
    assert run_helper(repo, "evil.example", {"PATH": os.environ["PATH"], "BITBUCKET_TOKEN": "tok-from-env", "GH_TOKEN": "gh-tok"}) == ""  # no helper is registered for it


def test_github_uses_gh_token_and_falls_back_to_github_token(tmp_path):
    r = tmp_path / "g"
    r.mkdir()
    git(r, "init", "-q", ".")
    git(r, "remote", "add", "origin", f"https://x-access-token:{SECRET}@github.com/org/repo.git")
    drop_stored_credentials(r, "https://github.com/org/repo.git", "x-access-token")
    base = {"PATH": os.environ["PATH"]}
    assert "password=gh-1" in run_helper(r, "github.com", {**base, "GH_TOKEN": "gh-1", "GITHUB_TOKEN": "gh-2"})
    assert "password=gh-2" in run_helper(r, "github.com", {**base, "GITHUB_TOKEN": "gh-2"})
    assert SECRET not in everything_under_git(r)


def test_unknown_hosts_are_cleaned_without_installing_a_helper(tmp_path):
    r = tmp_path / "s"
    r.mkdir()
    git(r, "init", "-q", ".")
    git(r, "remote", "add", "origin", f"https://u:{SECRET}@git.internal.example/o/r.git")
    drop_stored_credentials(r, "https://git.internal.example/o/r.git", "u")
    assert SECRET not in everything_under_git(r) and "helper" not in (r / ".git" / "config").read_text()


def test_a_hostile_username_cannot_inject_shell_into_the_helper(repo):
    drop_stored_credentials(repo, "https://bitbucket.org/ws/repo.git", "x; touch /tmp/pwned-by-test #")
    out = run_helper(repo, "bitbucket.org", {"PATH": os.environ["PATH"], "BITBUCKET_TOKEN": "t"})
    assert "password=t" in out and "username=x; touch /tmp/pwned-by-test #" in out and not Path("/tmp/pwned-by-test").exists()
