"""Tests for scripts/slack/registry.py"""

import pytest

from scripts.slack.registry import Registry, WorkflowEntry, _infer_target_kind


@pytest.fixture
def registry(tmp_path):
    """Registry backed by minimal fixture YAML files."""
    wf_yaml = tmp_path / "workflows.yml"
    wf_yaml.write_text("""
workflows:
  - name: gh-implement
    job_type: ai-gh-implement
    shape: ai-implement
    triggers: ["implement", "build", "gh issue"]
    skill: ""
    id_var: IssueNumber
    required_vars: [IssueNumber]
    target_kind: github
    description: Implement a GitHub issue

  - name: jira-implement
    job_type: ai-jira-implement
    shape: ai-implement
    triggers: ["implement", "build", "jira issue"]
    skill: ""
    id_var: IssueNumber
    required_vars: [IssueNumber]
    target_kind: jira
    description: Implement a Jira issue

  - name: gh-review
    job_type: ai-gh-review
    shape: ai-review
    triggers: ["review", "code review", "pr review"]
    skill: ygs-review-pr
    id_var: PRUrl
    required_vars: [PRUrl]
    target_kind: github
    description: Review a GitHub PR

  - name: standup
    job_type: ai-adhoc
    shape: ai-adhoc
    triggers: ["standup", "status", "daily"]
    skill: ygs-standup
    id_var: Prompt
    required_vars: []
    target_kind: any
    description: Run standup
""")
    sk_yaml = tmp_path / "skills.yml"
    sk_yaml.write_text("""
skills:
  - name: ygs-standup
    source: github.com/bhatti/you-got-skills
    path: skills/ygs-standup
    ref: main
    description: Standup skill
  - name: ygs-review-pr
    source: github.com/bhatti/you-got-skills
    path: skills/ygs-review-pr
    ref: main
    description: PR review skill
""")
    return Registry(wf_yaml, sk_yaml)


# ---------------------------------------------------------------------------
# _infer_target_kind
# ---------------------------------------------------------------------------

def test_infer_jira_key():
    assert _infer_target_kind("PROJ-123") == "jira"
    assert _infer_target_kind("ABC-1") == "jira"


def test_infer_github_pr_url():
    assert _infer_target_kind("https://github.com/org/repo/pull/42") == "github"


def test_infer_github_issue_url():
    assert _infer_target_kind("https://github.com/org/repo/issues/10") == "github"


def test_infer_bitbucket_url():
    assert _infer_target_kind("https://bitbucket.org/workspace/repo/pull-requests/5") == "jira"


def test_infer_empty_string():
    assert _infer_target_kind("") == "any"


def test_infer_numeric_id_is_any():
    assert _infer_target_kind("42") == "any"


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------

def test_resolve_exact_target_kind_wins(registry):
    """When both github and jira match 'implement', exact target_kind wins."""
    gh_entry = registry.resolve("implement", target_kind="github")
    jira_entry = registry.resolve("implement", target_kind="jira")
    assert gh_entry is not None
    assert jira_entry is not None
    assert gh_entry.job_type == "ai-gh-implement"
    assert jira_entry.job_type == "ai-jira-implement"


def test_resolve_any_kind_fallback(registry):
    """Standup has target_kind=any — resolves regardless of target_kind."""
    entry = registry.resolve("standup", target_kind="github")
    assert entry is not None
    assert entry.job_type == "ai-adhoc"


def test_resolve_returns_none_for_unknown(registry):
    assert registry.resolve("deploy to prod") is None


def test_resolve_case_insensitive(registry):
    entry = registry.resolve("STANDUP", target_kind="any")
    assert entry is not None
    assert entry.skill == "ygs-standup"


def test_resolve_partial_trigger_match(registry):
    """'code review' substring matches trigger containing 'code review'."""
    entry = registry.resolve("code review", target_kind="github")
    assert entry is not None
    assert entry.job_type == "ai-gh-review"


# ---------------------------------------------------------------------------
# parse_verb()
# ---------------------------------------------------------------------------

def test_parse_verb_review_gh_pr(registry):
    result = registry.parse_verb("review", "https://github.com/org/repo/pull/42")
    assert result is not None
    intent, target_kind, entity_id = result
    assert intent == "review"
    assert target_kind == "github"
    assert entity_id == "https://github.com/org/repo/pull/42"


def test_parse_verb_implement_jira(registry):
    result = registry.parse_verb("implement", "PROJ-123")
    assert result is not None
    intent, target_kind, entity_id = result
    assert intent == "implement"
    assert target_kind == "jira"
    assert entity_id == "PROJ-123"


def test_parse_verb_standup_no_entity(registry):
    result = registry.parse_verb("standup", "")
    assert result is not None
    intent, target_kind, entity_id = result
    assert intent == "standup"
    assert target_kind == "any"
    assert entity_id == ""


def test_parse_verb_unknown_returns_none(registry):
    result = registry.parse_verb("frobnicate", "something")
    assert result is None


def test_parse_verb_alias_pr_maps_to_review(registry):
    """'pr' alias should map to review intent."""
    result = registry.parse_verb("pr", "https://github.com/x/y/pull/1")
    assert result is not None
    assert result[0] == "review"


# ---------------------------------------------------------------------------
# missing_required_vars()
# ---------------------------------------------------------------------------

def test_missing_required_vars_satisfied(registry):
    entry = registry.resolve("review", target_kind="github")
    assert entry is not None
    missing = registry.missing_required_vars(entry, target_id="https://github.com/x/y/pull/1")
    assert missing == []


def test_missing_required_vars_empty_entity(registry):
    entry = registry.resolve("review", target_kind="github")
    assert entry is not None
    missing = registry.missing_required_vars(entry, target_id="")
    assert "PRUrl" in missing


def test_missing_required_vars_standup_always_empty(registry):
    entry = registry.resolve("standup", target_kind="any")
    assert entry is not None
    missing = registry.missing_required_vars(entry, target_id="")
    assert missing == []


# ---------------------------------------------------------------------------
# Skills loading
# ---------------------------------------------------------------------------

def test_skills_loaded(registry):
    assert len(registry.skills) == 2
    names = [s.name for s in registry.skills]
    assert "ygs-standup" in names
    assert "ygs-review-pr" in names


def test_from_default_loads_without_error():
    """from_default() resolves correctly when called in installed package."""
    reg = Registry.from_default()
    assert len(reg.workflows) > 0
    assert len(reg.skills) > 0
