"""Unit tests for scripts.skill.flags — flag parsing and repo/branch/tracker resolution."""

from __future__ import annotations

import pytest

from scripts.skill.flags import SkillFlags, parse_skill_flags, resolve_repo, resolve_tracker


# ─── parse_skill_flags ────────────────────────────────────────────────────────


class TestParseSkillFlags:
    def test_skill_name_only(self):
        f = parse_skill_flags("ygs-analyze")
        assert f.skill == "ygs-analyze"
        assert f.repo == ""
        assert f.branch == ""
        assert f.tracker == ""
        assert f.service == ""
        assert f.model == ""
        assert f.instructions == ""

    def test_with_repo_url(self):
        f = parse_skill_flags("ygs-analyze --repo https://github.com/org/repo")
        assert f.skill == "ygs-analyze"
        assert f.repo == "https://github.com/org/repo"

    def test_with_short_repo_name(self):
        f = parse_skill_flags("ygs-analyze --repo myapp")
        assert f.skill == "ygs-analyze"
        assert f.repo == "myapp"

    def test_with_all_flags(self):
        f = parse_skill_flags(
            "ygs-qa --repo myapp --branch dev --tracker jira --model opus --service img:1.0"
        )
        assert f.skill == "ygs-qa"
        assert f.repo == "myapp"
        assert f.branch == "dev"
        assert f.tracker == "jira"
        assert f.model == "opus"
        assert f.service == "img:1.0"

    def test_with_instructions(self):
        f = parse_skill_flags("ygs-qa --repo myapp -- run E2E tests against leader")
        assert f.skill == "ygs-qa"
        assert f.repo == "myapp"
        assert f.instructions == "run E2E tests against leader"

    def test_instructions_with_no_flags(self):
        f = parse_skill_flags("ygs-investigate -- investigate flaky test JIRA-123")
        assert f.skill == "ygs-investigate"
        assert f.repo == ""
        assert f.instructions == "investigate flaky test JIRA-123"

    def test_empty_string(self):
        f = parse_skill_flags("")
        assert f.skill == ""
        assert f.instructions == ""

    def test_none_input(self):
        f = parse_skill_flags(None)
        assert f.skill == ""

    def test_no_double_dash(self):
        f = parse_skill_flags("ygs-analyze --repo myapp --branch dev")
        assert f.instructions == ""

    def test_tracker_lowercase(self):
        f = parse_skill_flags("ygs-qa --tracker GitHub")
        assert f.tracker == "github"

    def test_org_slash_repo(self):
        f = parse_skill_flags("ygs-analyze --repo org/myrepo")
        assert f.repo == "org/myrepo"

    def test_multiple_spaces(self):
        f = parse_skill_flags("  ygs-analyze   --repo  myapp   --branch  dev  ")
        assert f.skill == "ygs-analyze"
        assert f.repo == "myapp"
        assert f.branch == "dev"

    def test_double_dash_with_empty_instructions(self):
        f = parse_skill_flags("ygs-analyze --repo myapp -- ")
        assert f.skill == "ygs-analyze"
        assert f.instructions == ""

    # ── positional args ──

    def test_positional_repo_and_id(self):
        f = parse_skill_flags("review-pr myapp 4444")
        assert f.skill == "review-pr"
        assert f.repo == "myapp"
        assert f.identifier == "4444"
        assert f.instructions == ""

    def test_positional_repo_id_with_flags(self):
        f = parse_skill_flags("review-pr myapp 4444 --branch release")
        assert f.skill == "review-pr"
        assert f.repo == "myapp"
        assert f.identifier == "4444"
        assert f.branch == "release"

    def test_positional_repo_only(self):
        f = parse_skill_flags("analyze myapp")
        assert f.skill == "analyze"
        assert f.repo == "myapp"
        assert f.identifier == ""

    def test_positional_with_instructions(self):
        f = parse_skill_flags("analyze myapp -- focus on test coverage")
        assert f.skill == "analyze"
        assert f.repo == "myapp"
        assert f.instructions == "focus on test coverage"

    def test_positional_words_become_instructions(self):
        f = parse_skill_flags("ask what is the deployment process")
        assert f.skill == "ask"
        assert f.instructions == "is the deployment process"
        assert f.repo == "what"  # first non-numeric becomes repo

    def test_positional_all_words_no_flags(self):
        """When no flags at all, first word=skill, second=repo, rest=instructions."""
        f = parse_skill_flags("ask -- what is the deployment process")
        assert f.skill == "ask"
        assert f.repo == ""
        assert f.instructions == "what is the deployment process"

    def test_explicit_repo_flag_wins_over_positional(self):
        f = parse_skill_flags("review-pr --repo https://github.com/org/repo 4444")
        assert f.repo == "https://github.com/org/repo"
        assert f.identifier == "4444"

    def test_positional_id_only(self):
        f = parse_skill_flags("review-pr 4444")
        assert f.skill == "review-pr"
        assert f.identifier == "4444"
        assert f.repo == ""

    def test_passthrough_unknown_flags(self):
        """Unknown --flags trigger passthrough: remainder becomes instructions verbatim."""
        f = parse_skill_flags(
            "run-tests unit --workers 4 --dry-run --max-failures 10 --branch my-feature"
        )
        assert f.skill == "run-tests"
        assert f.branch == "my-feature"
        assert f.repo == ""
        assert f.identifier == ""
        assert f.instructions == "unit --workers 4 --dry-run --max-failures 10"

    def test_passthrough_boolean_unknown_flag(self):
        """Boolean unknown flags (no value) also trigger passthrough mode."""
        f = parse_skill_flags("my-skill do-thing --dry-run --branch feat")
        assert f.skill == "my-skill"
        assert f.branch == "feat"
        assert f.repo == ""
        assert f.instructions == "do-thing --dry-run"

    def test_passthrough_no_known_flags(self):
        """Unknown flags with no known framework flags — all become instructions."""
        f = parse_skill_flags("my-skill --dry-run --count 5")
        assert f.skill == "my-skill"
        assert f.repo == ""
        assert f.branch == ""
        assert f.instructions == "--dry-run --count 5"

    def test_passthrough_with_tracker_flag(self):
        """Known --tracker is extracted; unknown flags still become instructions."""
        f = parse_skill_flags("my-skill --dry-run --tracker github")
        assert f.skill == "my-skill"
        assert f.tracker == "github"
        assert f.repo == ""
        assert f.instructions == "--dry-run"

    def test_passthrough_flag_with_known_prefix(self):
        """--reponame is not the known --repo flag and must trigger passthrough."""
        f = parse_skill_flags("my-skill --reponame foo")
        assert f.skill == "my-skill"
        assert f.repo == ""
        assert f.instructions == "--reponame foo"

    def test_passthrough_double_dash_instructions_merge(self):
        """-- separator instructions are appended after passthrough remainder."""
        f = parse_skill_flags("my-skill --dry-run -- run all tests")
        assert f.skill == "my-skill"
        assert f.instructions == "--dry-run run all tests"


# ─── resolve_tracker ──────────────────────────────────────────────────────────


class TestResolveTracker:
    def test_explicit_flag_wins(self):
        flags = SkillFlags(tracker="github")
        assert resolve_tracker(flags, {"DEFAULT_TRACKER": "jira"}) == "github"

    def test_github_url_detection(self):
        flags = SkillFlags(repo="https://github.com/org/repo")
        assert resolve_tracker(flags, {}) == "github"

    def test_bitbucket_url_detection(self):
        flags = SkillFlags(repo="https://bitbucket.org/ws/repo")
        assert resolve_tracker(flags, {}) == "jira"

    def test_fallback_to_default_tracker(self):
        flags = SkillFlags()
        assert resolve_tracker(flags, {"DEFAULT_TRACKER": "jira"}) == "jira"

    def test_fallback_to_jira_when_no_config(self):
        flags = SkillFlags()
        assert resolve_tracker(flags, {}) == "jira"

    def test_explicit_overrides_url(self):
        flags = SkillFlags(tracker="jira", repo="https://github.com/org/repo")
        assert resolve_tracker(flags, {}) == "jira"


# ─── resolve_repo ─────────────────────────────────────────────────────────────


class TestResolveRepo:
    def test_full_github_url(self):
        flags = SkillFlags(repo="https://github.com/org/repo.git")
        url, branch = resolve_repo(flags, {}, "github")
        assert url == "https://github.com/org/repo.git"
        assert branch == "main"

    def test_full_bitbucket_url(self):
        flags = SkillFlags(repo="https://bitbucket.org/ws/repo.git")
        url, branch = resolve_repo(flags, {}, "jira")
        assert url == "https://bitbucket.org/ws/repo.git"
        assert branch == "dev"

    def test_bare_name_github(self):
        flags = SkillFlags(repo="todo-sample")
        config = {"GH_ORG": "bhatti"}
        url, branch = resolve_repo(flags, config, "github")
        assert url == "https://github.com/bhatti/todo-sample.git"
        assert branch == "main"

    def test_bare_name_jira(self):
        flags = SkillFlags(repo="myapp")
        config = {"BITBUCKET_WORKSPACE": "myapp"}
        url, branch = resolve_repo(flags, config, "jira")
        assert url == "https://bitbucket.org/myapp/myapp.git"
        assert branch == "dev"

    def test_org_slash_name_github(self):
        flags = SkillFlags(repo="myorg/myrepo")
        url, branch = resolve_repo(flags, {}, "github")
        assert url == "https://github.com/myorg/myrepo.git"

    def test_org_slash_name_bitbucket(self):
        flags = SkillFlags(repo="myws/myrepo")
        url, branch = resolve_repo(flags, {}, "jira")
        assert url == "https://bitbucket.org/myws/myrepo.git"

    def test_no_repo_uses_org_config_github(self):
        flags = SkillFlags()
        config = {"GH_ORG": "bhatti", "GH_REPO": "todo-sample"}
        url, branch = resolve_repo(flags, config, "github")
        assert url == "https://github.com/bhatti/todo-sample.git"
        assert branch == "main"

    def test_no_repo_uses_org_config_bitbucket(self):
        flags = SkillFlags()
        config = {"BITBUCKET_WORKSPACE": "myapp", "BITBUCKET_REPO": "myapp"}
        url, branch = resolve_repo(flags, config, "jira")
        assert url == "https://bitbucket.org/myapp/myapp.git"
        assert branch == "dev"

    def test_no_repo_no_config_returns_none(self):
        flags = SkillFlags()
        url, branch = resolve_repo(flags, {}, "github")
        assert url is None

    def test_explicit_branch(self):
        flags = SkillFlags(repo="myapp", branch="release-2.0")
        config = {"BITBUCKET_WORKSPACE": "myapp"}
        url, branch = resolve_repo(flags, config, "jira")
        assert branch == "release-2.0"

    def test_branch_from_env(self):
        flags = SkillFlags()
        config = {
            "GH_ORG": "bhatti",
            "GH_REPO": "todo-sample",
            "GIT_BRANCH": "feature-x",
        }
        url, branch = resolve_repo(flags, config, "github")
        assert branch == "feature-x"

    def test_branch_from_org_config_github(self):
        flags = SkillFlags()
        config = {
            "GH_ORG": "bhatti",
            "GH_REPO": "todo-sample",
            "GH_REPO_BRANCH": "develop",
        }
        url, branch = resolve_repo(flags, config, "github")
        assert branch == "develop"

    def test_branch_from_org_config_bitbucket(self):
        flags = SkillFlags()
        config = {
            "BITBUCKET_WORKSPACE": "myapp",
            "BITBUCKET_REPO": "myapp",
            "BB_REPO_BRANCH": "staging",
        }
        url, branch = resolve_repo(flags, config, "jira")
        assert branch == "staging"

    def test_codebase_repo_url_env(self):
        flags = SkillFlags()
        config = {"CODEBASE_REPO_URL": "https://github.com/org/repo.git"}
        url, branch = resolve_repo(flags, config, "github")
        assert url == "https://github.com/org/repo.git"

    def test_codebase_repo_url_no_value_ignored(self):
        flags = SkillFlags()
        config = {"CODEBASE_REPO_URL": "<no value>", "GH_ORG": "bhatti", "GH_REPO": "todo"}
        url, branch = resolve_repo(flags, config, "github")
        assert url == "https://github.com/bhatti/todo.git"

    def test_bare_name_no_org_returns_none(self):
        flags = SkillFlags(repo="myapp")
        url, branch = resolve_repo(flags, {}, "jira")
        assert url is None

    def test_git_at_url(self):
        flags = SkillFlags(repo="git@github.com:org/repo.git")
        url, branch = resolve_repo(flags, {}, "github")
        assert url == "git@github.com:org/repo.git"
