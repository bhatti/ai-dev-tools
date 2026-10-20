"""Unit tests for scripts.skill.flags — flag parsing and repo/branch/tracker resolution."""

from __future__ import annotations

import pytest

from scripts.skill.flags import SkillFlags, github_identity, infer_repo_url, parse_skill_flags, resolve_repo, resolve_tracker


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

    # ── --turns flag ──

    def test_turns_flag_extracted(self):
        f = parse_skill_flags("my-skill --turns 200")
        assert f.skill == "my-skill"
        assert f.turns == "200"
        assert f.repo == ""

    def test_turns_flag_does_not_trigger_passthrough(self):
        """--turns is a known framework flag, should not trigger passthrough mode."""
        f = parse_skill_flags("my-skill some-target --turns 150 --branch feat")
        assert f.turns == "150"
        assert f.branch == "feat"
        assert f.repo == "some-target"
        assert f.instructions == ""

    def test_turns_flag_combined_with_unknown_flags(self):
        """--turns extracted cleanly when other unknown flags trigger passthrough."""
        f = parse_skill_flags("my-skill --turns 200 --dry-run --branch feat")
        assert f.turns == "200"
        assert f.branch == "feat"
        assert f.instructions == "--dry-run"

    def test_turns_flag_invalid_value_stored_as_string(self):
        """Invalid --turns value is stored as-is; caller validates at use time."""
        f = parse_skill_flags("my-skill --turns abc")
        assert f.turns == "abc"


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


# ─── service flags ───────────────────────────────────────────────────────────

class TestServiceFlags:
    def test_service_image_parsed(self):
        flags = parse_skill_flags("ygs-ask --service nginx:alpine")
        assert flags.service == "nginx:alpine"

    def test_service_port_parsed(self):
        flags = parse_skill_flags("ygs-ask --service nginx:alpine --service-port 8080")
        assert flags.service_port == "8080"

    def test_service_cmd_single_word(self):
        flags = parse_skill_flags("ygs-ask --service-cmd formicary")
        assert flags.service_cmd == "formicary"

    def test_service_cmd_quoted_multi_word(self):
        # --service-cmd "formicary queen" should capture the full quoted string.
        flags = parse_skill_flags('integ-tests --service-cmd "formicary queen" --service-port 7777')
        assert flags.service_cmd == "formicary queen"
        assert flags.service_port == "7777"

    def test_service_args_parsed(self):
        flags = parse_skill_flags("integ-tests --service-args --standalone")
        assert flags.service_args == "--standalone"

    def test_service_args_quoted(self):
        flags = parse_skill_flags('integ-tests --service-args "--standalone --no-auth"')
        assert flags.service_args == "--standalone --no-auth"

    def test_service_cmd_does_not_bleed_into_repo(self):
        # Without quotes, "queen" is a second token; should NOT become repo.
        # With quotes, the whole value is captured and repo stays empty.
        flags = parse_skill_flags('integ-tests --service-cmd "formicary queen"')
        assert flags.service_cmd == "formicary queen"
        assert flags.repo == ""


# ─── quoted positional args ───────────────────────────────────────────────────
# Reproduces: skill with quoted multi-word positional arg and unknown flag
# Previously broke YAML rendering (unescaped double-quotes) AND positional tokenization.

class TestQuotedPositionalArgs:
    def test_quoted_arg_in_passthrough_mode(self):
        """Quoted multi-word positional arg is stripped of quotes and merged into instructions."""
        f = parse_skill_flags(
            'my-sprint-skill "Q4 Roadmap" --dry-run --branch feature-branch'
        )
        assert f.skill == "my-sprint-skill"
        assert f.branch == "feature-branch"
        # shlex strips quotes; --dry-run is an unknown flag → passthrough
        assert "Q4 Roadmap" in f.instructions
        assert "--dry-run" in f.instructions
        assert '"' not in f.instructions  # no stray quote chars

    def test_quoted_arg_in_positional_mode(self):
        """Quoted first positional arg is treated as a single repo token."""
        f = parse_skill_flags('my-sprint-skill "Q4 Roadmap"')
        assert f.skill == "my-sprint-skill"
        # No unknown flags → positional mode; first token becomes repo
        assert f.repo == "Q4 Roadmap"
        assert f.identifier == ""
        assert f.instructions == ""

    def test_quoted_arg_with_instructions_separator(self):
        """Quoted arg before ' -- ' separator is in flag part, not instructions."""
        f = parse_skill_flags('my-sprint-skill "Q4 Roadmap" -- focus on blockers')
        assert f.skill == "my-sprint-skill"
        assert f.repo == "Q4 Roadmap"
        assert f.instructions == "focus on blockers"

    def test_single_quoted_arg(self):
        """Single-quoted multi-word arg is also handled correctly."""
        f = parse_skill_flags("my-sprint-skill 'Sprint 203' --branch main")
        assert f.skill == "my-sprint-skill"
        assert f.repo == "Sprint 203"
        assert f.branch == "main"

    def test_unmatched_quote_falls_back_to_split(self):
        """Malformed quotes fall back to whitespace split without raising."""
        f = parse_skill_flags('my-sprint-skill "broken --branch main')
        assert f.skill == "my-sprint-skill"
        assert f.branch == "main"
        # No crash — graceful fallback


class TestDindFlag:
    def test_dind_sets_flag(self):
        f = parse_skill_flags("qa-regression --repo org/myapp --branch main --dind")
        assert f.dind is True
        assert f.skill == "qa-regression"
        assert f.repo == "org/myapp"
        assert f.branch == "main"
        assert "--dind" not in (f.instructions or "")

    def test_dind_alone(self):
        f = parse_skill_flags("qa-regression --dind")
        assert f.dind is True
        assert f.skill == "qa-regression"

    def test_no_dind_flag(self):
        f = parse_skill_flags("qa-regression --repo org/repo")
        assert f.dind is False

    def test_dind_with_instructions(self):
        f = parse_skill_flags("qa-regression --dind -- run full suite")
        assert f.dind is True
        assert f.instructions == "run full suite"
        assert "--dind" not in f.instructions

    def test_dind_does_not_trigger_passthrough(self):
        """--dind is a known boolean flag; it must NOT trigger passthrough mode."""
        f = parse_skill_flags("qa-regression myapp --dind --branch feat")
        assert f.dind is True
        assert f.repo == "myapp"
        assert f.branch == "feat"
        assert "--dind" not in (f.instructions or "")

    def test_dind_combined_with_service(self):
        f = parse_skill_flags("integ --service nginx:alpine --dind --branch main")
        assert f.dind is True
        assert f.service == "nginx:alpine"
        assert f.branch == "main"

    def test_dind_in_instructions_not_parsed(self):
        """--dind after the ' -- ' separator is instructions text, not a framework flag."""
        f = parse_skill_flags("ygs-qa -- use --dind for docker")
        assert f.dind is False
        assert f.instructions == "use --dind for docker"

    def test_dind_no_false_positive_on_similar_flag(self):
        """--dind-extra must NOT set flags.dind (exact token match required)."""
        f = parse_skill_flags("some-skill --dind-extra --branch main")
        # --dind-extra is unknown → passthrough mode, dind must stay False
        assert f.dind is False
        assert "--dind-extra" in f.instructions


class TestUrlInference:
    """A PR/issue url names its repo: no --repo needed, and Slack's <url|label> wrapper is stripped."""

    def test_github_pr_url_sets_repo_and_stays_in_instructions(self):
        f = parse_skill_flags("ygs-qa https://github.com/org/repo/pull/12 --url http://staging")
        assert f.repo == "https://github.com/org/repo.git"
        assert "https://github.com/org/repo/pull/12" in f.instructions and "--url http://staging" in f.instructions

    def test_bitbucket_pr_url_with_overview_suffix(self):
        f = parse_skill_flags("ygs-qa https://bitbucket.org/ws/repo/pull-requests/12/overview")
        assert f.repo == "https://bitbucket.org/ws/repo.git"
        assert resolve_tracker(f, {"DEFAULT_TRACKER": "github"}) == "jira"

    def test_slack_wrapped_link_is_unwrapped(self):
        raw = ("ygs-qa <https://bitbucket.org/example-org/example-repo/pull-requests/12/overview|bitbucket.org/example-org/example-repo/pull-requests/12/overview> "
               "--version 1.2.3-rc1 --branch feature/x")
        f = parse_skill_flags(raw)
        assert f.repo == "https://bitbucket.org/example-org/example-repo.git" and f.branch == "feature/x"
        assert "<" not in f.instructions and "|" not in f.instructions
        assert f.instructions.startswith("https://bitbucket.org/example-org/example-repo/pull-requests/12/overview")
        assert "--version 1.2.3-rc1" in f.instructions

    def test_explicit_repo_wins_over_the_url(self):
        f = parse_skill_flags("ygs-qa https://github.com/org/repo/pull/12 --repo https://github.com/fork/other.git")
        assert f.repo == "https://github.com/fork/other.git"

    def test_github_issue_url(self):
        assert parse_skill_flags("ygs-flaky-test https://github.com/org/repo/issues/42").repo == "https://github.com/org/repo.git"

    def test_repo_flag_given_a_pr_url_is_normalised(self):
        f = parse_skill_flags("ygs-qa --repo https://github.com/org/repo/pull/12 --url http://x")
        assert f.repo == "https://github.com/org/repo.git" and "pull/12" in f.instructions

    def test_jira_url_and_plain_text_infer_nothing(self):
        assert parse_skill_flags("ygs-qa https://example.atlassian.net/browse/ABC-1 --url http://x").repo == ""
        assert parse_skill_flags("ygs-qa -- review the changes in github.com/org/repo").repo == ""

    def test_infer_repo_url_direct(self):
        assert infer_repo_url("https://github.com/a/b.git/pull/3") == "https://github.com/a/b.git"
        assert infer_repo_url("see https://github.com/a/b/pull/3 now") == ""  # must *be* the url, not contain one
        assert infer_repo_url("") == ""


class TestUrlInferenceIsConservative:
    def test_a_link_inside_free_text_never_changes_the_repository(self):
        """A reference link in prose must not make the runner clone another repo with the user's token."""
        for raw in ("ygs-qa -- check https://github.com/evil/x/issues/1 and then", "ygs-qa --url http://staging https://github.com/evil/x/pull/1",
                    "ygs-flaky-test -- the checkout test, see https://github.com/evil/x/issues/9"):
            assert parse_skill_flags(raw).repo == "", raw

    def test_only_the_first_argument_after_the_skill_name_counts(self):
        assert parse_skill_flags("ygs-qa https://github.com/org/repo/pull/12 --url http://x").repo == "https://github.com/org/repo.git"
        assert parse_skill_flags("ygs-qa --baseline-url http://x https://github.com/org/repo/pull/12").repo == ""


class TestIssueKeyArgument:
    def test_a_jira_key_is_an_identifier_not_a_repository(self):
        f = parse_skill_flags("ygs-qa ABC-123")
        assert f.identifier == "ABC-123" and f.repo == ""

    def test_key_next_to_an_explicit_repo(self):
        f = parse_skill_flags("ygs-flaky-test PLAT-9 --repo org/repo")
        assert f.identifier == "PLAT-9" and f.repo == "org/repo"

    def test_a_repo_named_like_a_key_with_lowercase_is_still_a_repo(self):
        assert parse_skill_flags("ygs-analyze my-app-1").repo == "my-app-1"


class TestInferenceIsAnchored:
    @pytest.mark.parametrize("arg", ["https://evil.example/?u=https://github.com/a/b/pull/1", "http://github.com/a/b/pull/1",
                                     "https://github.com.evil.example/a/b/pull/1", "xhttps://github.com/a/b/pull/1"])
    def test_urls_that_merely_contain_a_pr_url_do_not_name_the_repo(self, arg):
        assert parse_skill_flags(f"ygs-qa {arg}").repo != "https://github.com/a/b.git"
        assert infer_repo_url(arg) == ""


class TestPrUrlSkillPrompt:
    """The Slack prompt `skill <name> <pr url> --version <v> --branch <b> --dind` as the router hands it over."""
    RAW = "qa-regression https://bitbucket.org/acme/widgets/pull-requests/7/overview --version 4.20.2-TC1 --branch my-branch --dind"

    def test_flags(self):
        f = parse_skill_flags(self.RAW)
        assert (f.skill, f.branch, f.dind) == ("qa-regression", "my-branch", True)
        assert "bitbucket.org/acme/widgets/pull-requests/7" in f.instructions
        assert "--version 4.20.2-TC1" in f.instructions
        assert "--dind" not in f.instructions and "--branch" not in f.instructions

    def test_tracker_and_repo_are_inferred_from_the_pr_url(self):
        flags = parse_skill_flags(self.RAW)
        tracker = resolve_tracker(flags, {"DEFAULT_TRACKER": "github"})
        assert tracker == "jira"  # Bitbucket repos are served by the jira/bitbucket tracker
        url, branch = resolve_repo(flags, {"DEFAULT_TRACKER": "github"}, tracker)
        assert (url, branch) == ("https://bitbucket.org/acme/widgets.git", "my-branch")


class TestTicketSkillPrompts:
    """`skill ygs-sprint-ready ...` / `skill ygs-enrich-ticket ...` as Slack hands them over."""

    def test_sprint_name_with_spaces_and_the_ticket_flags_stay_in_the_instructions(self):
        f = parse_skill_flags('ygs-sprint-ready --sprint "Sprint 12" --dry-run --tracker github')
        assert (f.skill, f.tracker) == ("ygs-sprint-ready", "github")
        # Slack quoting is stripped by design, so the skills read the value after --sprint up to the next --flag
        assert f.instructions == "--sprint Sprint 12 --dry-run"

    def test_a_jira_key_or_issue_url_and_apply_are_instructions_not_flags(self):
        f = parse_skill_flags("ygs-enrich-ticket PROJ-123 --apply")
        assert f.skill == "ygs-enrich-ticket" and f.instructions == "PROJ-123 --apply" and f.repo == ""

    def test_a_github_issue_url_names_the_repo_and_the_tracker(self):
        f = parse_skill_flags("ygs-enrich-ticket https://github.com/acme/widgets/issues/42")
        assert resolve_tracker(f, {"DEFAULT_TRACKER": "jira"}) == "github"

    def test_a_github_milestone_prompt_keeps_every_ticket_flag_and_the_tracker_is_consumed(self):
        f = parse_skill_flags('ygs-sprint-ready --tracker github --milestone "Sprint 12" --apply --format json --repo acme/widgets')
        assert f.tracker == "github" and f.repo == "acme/widgets"
        assert f.instructions == "--milestone Sprint 12 --apply --format json"


class TestExplicitRepoIdentity:
    @pytest.mark.parametrize("url,expected", [("https://github.com/acme/widgets.git", ("acme", "widgets")), ("git@github.com:acme/widgets.git", ("acme", "widgets")),
                                              ("https://github.com/acme/widgets", ("acme", "widgets")), ("https://github.com/acme/widgets/tree/main/docs", ("acme", "widgets")),
                                              ("https://github.com/acme/widgets/pull/7", ("acme", "widgets")), ("https://bitbucket.org/ws/repo.git", None), (None, None), ("", None)])
    def test_github_identity(self, url, expected):
        assert github_identity(url) == expected
