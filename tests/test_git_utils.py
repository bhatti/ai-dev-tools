"""Tests for scripts/common/git_utils.py"""

from unittest.mock import MagicMock, call, patch

import pytest

from scripts.common.git_utils import (
    _git_env,
    _run_git,
    _slug,
    commit_all,
    create_branch,
    get_commit_count,
    make_branch_name,
    normalize_repo_web_url,
    resolve_clone_auth,
    sparse_clone_repo,
)


def test_slug_basic():
    assert _slug("Add user authentication") == "add-user-authentication"


def test_slug_special_chars():
    assert _slug("Fix bug: remove #1 issue!") == "fix-bug-remove-1-issue"


def test_slug_truncates():
    long = "a" * 100
    assert len(_slug(long)) <= 40


def test_make_branch_name():
    branch = make_branch_name("42", "Add login feature", nonce="abc123")
    assert branch == "ai/42-add-login-feature-abc123"


def test_make_branch_name_long_title():
    branch = make_branch_name("42", "A" * 100, nonce="abc")
    # branch should be ai/42-{slug}-abc and slug <= 40 chars
    parts = branch.split("-")
    assert branch.startswith("ai/42-")


def test_make_branch_name_generates_nonce():
    b1 = make_branch_name("42", "title")
    b2 = make_branch_name("42", "title")
    # nonce should differ (random)
    assert b1 != b2


@patch("scripts.common.git_utils._run")
def test_commit_all_nothing_to_commit(mock_run):
    mock_run.side_effect = [
        MagicMock(returncode=0),  # git add -A
        MagicMock(stdout="", returncode=0),  # git status --porcelain (empty = nothing)
    ]
    from pathlib import Path
    result = commit_all(Path("/fake/repo"), "test commit")
    assert result is False


@patch("scripts.common.git_utils._run")
def test_commit_all_with_changes(mock_run):
    mock_run.side_effect = [
        MagicMock(returncode=0),  # git add -A
        MagicMock(stdout=" M file.py\n", returncode=0),  # git status --porcelain
        MagicMock(returncode=0),  # git commit
    ]
    from pathlib import Path
    result = commit_all(Path("/fake/repo"), "test commit")
    assert result is True


@patch("scripts.common.git_utils._run")
def test_get_commit_count_success(mock_run):
    mock_run.return_value = MagicMock(stdout="3\n", returncode=0)
    from pathlib import Path
    count = get_commit_count(Path("/fake/repo"), "main")
    assert count == 3


@patch("scripts.common.git_utils._run")
def test_get_commit_count_git_error(mock_run):
    mock_run.return_value = MagicMock(stdout="", returncode=1)
    from pathlib import Path
    count = get_commit_count(Path("/fake/repo"), "main")
    assert count == 0


@patch("scripts.common.git_utils._run")
def test_get_commit_count_uses_origin_ref_first(mock_run):
    """Uses origin/<base> first so shallow clones without local branch work."""
    # First call: origin/stage ref succeeds
    mock_run.return_value = MagicMock(stdout="5\n", returncode=0)
    from pathlib import Path
    count = get_commit_count(Path("/fake/repo"), "stage")
    assert count == 5
    # Should have tried origin/stage first
    first_call_cmd = mock_run.call_args_list[0][0][0]
    assert any("origin/stage..HEAD" in arg for arg in first_call_cmd)


@patch("scripts.common.git_utils._run")
def test_get_commit_count_falls_back_to_local_branch(mock_run):
    """Falls back to bare branch name when origin/ ref fails."""
    mock_run.side_effect = [
        MagicMock(stdout="", returncode=1),   # origin/stage fails
        MagicMock(stdout="3\n", returncode=0), # bare 'stage' succeeds
    ]
    from pathlib import Path
    count = get_commit_count(Path("/fake/repo"), "stage")
    assert count == 3


# ── create_branch tests ───────────────────────────────────────────────────────

@patch("scripts.common.git_utils._run")
def test_create_branch_already_local(mock_run):
    """Reuses an existing local branch without touching origin."""
    mock_run.return_value = MagicMock(stdout="  ai/42-my-branch-abc\n", returncode=0)
    from pathlib import Path
    result = create_branch(Path("/repo"), "ai/42-my-branch-abc")
    assert result == "ai/42-my-branch-abc"
    # First call: git branch --list; second: git checkout
    assert mock_run.call_count == 2
    assert mock_run.call_args_list[1][0][0] == ["git", "checkout", "ai/42-my-branch-abc"]


@patch("scripts.common.git_utils._run")
def test_create_branch_new_no_base(mock_run):
    """Creates a new branch from current HEAD when no base_branch given."""
    mock_run.side_effect = [
        MagicMock(stdout="", returncode=0),          # branch --list (empty = not local)
        MagicMock(stdout="", returncode=1),           # rev-parse tracking ref (absent)
        MagicMock(stdout="", returncode=0),           # ls-remote (empty = not on remote)
        MagicMock(returncode=0),                      # checkout -b
    ]
    from pathlib import Path
    result = create_branch(Path("/repo"), "ai/42-my-branch-abc")
    assert result == "ai/42-my-branch-abc"
    last_call = mock_run.call_args_list[-1][0][0]
    assert last_call == ["git", "checkout", "-b", "ai/42-my-branch-abc"]


@patch("scripts.common.git_utils._run")
def test_create_branch_new_with_base_already_fetched(mock_run):
    """Forks new branch from origin/<base_branch> when tracking ref already exists."""
    mock_run.side_effect = [
        MagicMock(stdout="", returncode=0),          # branch --list (not local)
        MagicMock(stdout="", returncode=1),           # rev-parse feature branch tracking (absent)
        MagicMock(stdout="", returncode=0),           # ls-remote feature branch (not on remote)
        MagicMock(stdout="abc123\n", returncode=0),  # rev-parse origin/stage (exists)
        MagicMock(returncode=0),                      # checkout -b from origin/stage
    ]
    from pathlib import Path
    result = create_branch(Path("/repo"), "ai/42-my-branch-abc", base_branch="stage")
    assert result == "ai/42-my-branch-abc"
    last_call = mock_run.call_args_list[-1][0][0]
    assert last_call == ["git", "checkout", "-b", "ai/42-my-branch-abc", "origin/stage"]


@patch("scripts.common.git_utils._run")
def test_create_branch_new_with_base_needs_fetch(mock_run):
    """Fetches origin/<base_branch> first when tracking ref is absent, then forks."""
    mock_run.side_effect = [
        MagicMock(stdout="", returncode=0),          # branch --list (not local)
        MagicMock(stdout="", returncode=1),           # rev-parse feature branch tracking (absent)
        MagicMock(stdout="", returncode=0),           # ls-remote feature branch (not on remote)
        MagicMock(stdout="", returncode=1),           # rev-parse origin/stage (absent)
        MagicMock(returncode=0),                      # fetch origin stage
        MagicMock(returncode=0),                      # checkout -b from origin/stage
    ]
    from pathlib import Path
    result = create_branch(Path("/repo"), "ai/42-my-branch-abc", base_branch="stage")
    assert result == "ai/42-my-branch-abc"
    # fetch call should include the stage refspec
    fetch_call = mock_run.call_args_list[-2][0][0]
    assert "fetch" in fetch_call
    assert "+refs/heads/stage:refs/remotes/origin/stage" in fetch_call
    # final checkout should reference origin/stage
    checkout_call = mock_run.call_args_list[-1][0][0]
    assert checkout_call == ["git", "checkout", "-b", "ai/42-my-branch-abc", "origin/stage"]


# ─── resolve_clone_auth ──────────────────────────────────────────────────────


def test_resolve_clone_auth_github():
    config = {"GH_TOKEN": "ghp_abc123"}
    token, username, ssh_key = resolve_clone_auth(config, "github")
    assert token == "ghp_abc123"
    assert username == "x-access-token"
    assert ssh_key == ""


def test_resolve_clone_auth_bitbucket():
    config = {"BITBUCKET_TOKEN": "ATATT_xyz", "BITBUCKET_USERNAME": "user@example.com"}
    token, username, ssh_key = resolve_clone_auth(config, "jira")
    assert token == "ATATT_xyz"
    assert username == "x-token-auth"


def test_resolve_clone_auth_bitbucket_app_password():
    config = {"BITBUCKET_APP_PASSWORD": "app_pw", "BITBUCKET_USERNAME": "user@example.com"}
    token, username, ssh_key = resolve_clone_auth(config, "jira/bitbucket")
    assert token == "app_pw"
    assert username == "user@example.com"


def test_resolve_clone_auth_ssh_fallback():
    config = {"SSH_PRIVATE_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----"}
    token, username, ssh_key = resolve_clone_auth(config, "github")
    assert token == ""
    assert ssh_key == "-----BEGIN OPENSSH PRIVATE KEY-----"


def test_resolve_clone_auth_auto_detect_tracker():
    config = {"DEFAULT_TRACKER": "github", "GH_TOKEN": "ghp_test"}
    token, username, ssh_key = resolve_clone_auth(config)
    assert token == "ghp_test"
    assert username == "x-access-token"


# ── normalize_repo_web_url ────────────────────────────────────────────────────

class TestNormalizeRepoWebUrl:
    def test_bitbucket_src_url_no_path(self):
        result = normalize_repo_web_url("https://bitbucket.org/example-org/example-repo/src/dev/")
        assert result == {"url": "https://bitbucket.org/example-org/example-repo.git", "branch": "dev"}

    def test_bitbucket_src_url_with_path(self):
        result = normalize_repo_web_url("https://bitbucket.org/example-org/example-repo/src/main/.claude/skills")
        assert result == {
            "url": "https://bitbucket.org/example-org/example-repo.git",
            "branch": "main",
            "skills_dir": ".claude/skills",
        }

    def test_github_tree_url_no_path(self):
        result = normalize_repo_web_url("https://github.com/org/repo/tree/feature-branch")
        assert result == {"url": "https://github.com/org/repo.git", "branch": "feature-branch"}

    def test_github_tree_url_with_path(self):
        result = normalize_repo_web_url("https://github.com/org/repo/tree/main/skills/ygs")
        assert result == {
            "url": "https://github.com/org/repo.git",
            "branch": "main",
            "skills_dir": "skills/ygs",
        }

    def test_github_blob_url(self):
        result = normalize_repo_web_url("https://github.com/org/repo/blob/main/README.md")
        assert result == {
            "url": "https://github.com/org/repo.git",
            "branch": "main",
            "skills_dir": "README.md",
        }

    def test_plain_git_url_unchanged(self):
        result = normalize_repo_web_url("https://bitbucket.org/example-org/example-repo.git")
        assert result == {"url": "https://bitbucket.org/example-org/example-repo.git"}

    def test_github_git_url_unchanged(self):
        result = normalize_repo_web_url("https://github.com/org/repo.git")
        assert result == {"url": "https://github.com/org/repo.git"}

    def test_non_hosting_url_unchanged(self):
        result = normalize_repo_web_url("https://example.com/my/repo.git")
        assert result == {"url": "https://example.com/my/repo.git"}


# ── _git_env / _run_git (shared helpers) ──────────────────────────────────────

class TestSharedGitHelpers:
    def test_git_env_disables_terminal_prompt(self):
        env = _git_env()
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_git_env_extra_keys_merged(self):
        env = _git_env({"MY_VAR": "val"})
        assert env["MY_VAR"] == "val"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_run_git_returns_on_success(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            result = _run_git(["git", "status"])
        assert result.returncode == 0

    def test_run_git_redacts_token_in_exception(self, tmp_path):
        import subprocess
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=128, stdout="", stderr="fatal: auth error"
            )
            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                _run_git(["git", "clone", "https://user:SUPERSECRET@bitbucket.org/org/repo.git", str(tmp_path)])
        cmd_str = str(exc_info.value.cmd)
        assert "SUPERSECRET" not in cmd_str

    def test_clone_repo_token_embedding_matches_sparse_clone(self, tmp_path):
        """clone_repo and sparse_clone_repo must embed BB/GH tokens identically."""
        from unittest.mock import call as _call
        token = "ATATT_test_token"
        username = "x-token-auth"
        dest = tmp_path / "repo"

        # clone_repo path
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            from scripts.common.git_utils import clone_repo
            clone_repo("https://bitbucket.org/org/repo.git", dest,
                       http_token=token, http_username=username, depth=1)
        clone_cmd = mock_run.call_args_list[0][0][0]
        clone_url = next(a for a in clone_cmd if token in a)

        # sparse_clone_repo path
        dest2 = tmp_path / "repo2"
        with patch("subprocess.run") as mock_run2:
            mock_run2.return_value = MagicMock(returncode=0, stdout="", stderr="")
            sparse_clone_repo("https://bitbucket.org/org/repo.git", dest2,
                              http_token=token, http_username=username)
        sparse_cmd = mock_run2.call_args_list[0][0][0]
        sparse_url = next(a for a in sparse_cmd if token in a)

        assert clone_url == sparse_url, (
            f"Token embedding differs between clone_repo ({clone_url!r}) "
            f"and sparse_clone_repo ({sparse_url!r})"
        )

    def test_clone_and_sparse_clone_set_git_terminal_prompt(self, tmp_path):
        """Both clone paths must pass GIT_TERMINAL_PROMPT=0 to suppress interactive prompts."""
        for fn_name, fn_args in [
            ("clone_repo", dict(url="https://github.com/org/repo.git", dest=tmp_path / "c1",
                                http_token="ghp_tok", depth=1)),
            ("sparse_clone_repo", dict(url="https://github.com/org/repo.git", dest=tmp_path / "c2",
                                       http_token="ghp_tok")),
        ]:
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                if fn_name == "clone_repo":
                    from scripts.common.git_utils import clone_repo
                    clone_repo(**fn_args)
                else:
                    sparse_clone_repo(**fn_args)
            all_kwargs = {**mock_run.call_args_list[0][1]}
            assert all_kwargs.get("env", {}).get("GIT_TERMINAL_PROMPT") == "0", \
                f"{fn_name} did not pass GIT_TERMINAL_PROMPT=0"


# ── sparse_clone_repo ─────────────────────────────────────────────────────────

class TestSparseCloneRepo:
    def test_skips_if_dest_already_exists(self, tmp_path):
        dest = tmp_path / "repo"
        dest.mkdir()
        (dest / ".git").mkdir()
        # No subprocess calls expected — should return immediately
        sparse_clone_repo("https://example.com/repo.git", dest)  # must not raise

    def test_calls_git_clone_with_sparse_flags(self, tmp_path):
        dest = tmp_path / "repo"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            sparse_clone_repo(
                "https://bitbucket.org/org/repo.git",
                dest,
                branch="dev",
                sparse_dir=".claude/skills",
            )
        calls = mock_run.call_args_list
        # First call: git clone
        clone_args = calls[0][0][0]
        assert "clone" in clone_args
        assert "--sparse" in clone_args
        assert "--depth" in clone_args
        assert "dev" in clone_args
        # Second call: sparse-checkout set
        checkout_args = calls[1][0][0]
        assert "sparse-checkout" in checkout_args
        assert ".claude/skills" in checkout_args

    def test_embeds_token_in_clone_url(self, tmp_path):
        dest = tmp_path / "repo"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            sparse_clone_repo(
                "https://bitbucket.org/org/repo.git",
                dest,
                http_token="ATATT_secret",
                http_username="x-token-auth",
                sparse_dir="",
            )
        clone_call = mock_run.call_args_list[0][0][0]
        clone_url_arg = next(a for a in clone_call if "ATATT_secret" in a)
        assert "x-token-auth" in clone_url_arg
        assert "ATATT_secret" in clone_url_arg

    def test_raises_on_clone_failure(self, tmp_path):
        import subprocess
        dest = tmp_path / "repo"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stderr="fatal: not found")
            with pytest.raises(subprocess.CalledProcessError):
                sparse_clone_repo("https://example.com/repo.git", dest)
