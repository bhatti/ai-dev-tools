"""Defect origin: was a bug fixed before or after release, with a confidence and the evidence."""

import pytest

from scripts.common import defect_origin as d
from scripts.common.pr_classify import build_metrics_dashboard
from scripts.common.pr_metrics import REGISTRY, build_single_pr_metrics


def bug(**kw):
    return {"pr_type": "bug", "title": "fix login", **kw}


class TestClassify:
    def test_a_pr_that_is_not_a_defect_fix_has_no_origin(self):
        assert d.classify({"pr_type": "feature", "title": "add x"}) is None
        assert d.cell({"pr_type": "feature", "title": "add x"}) == "—" and d.explain({"pr_type": "docs"}) == ""

    def test_no_evidence_means_assumed_post_release_with_low_confidence(self):
        o = d.classify(bug())
        assert (o.kind, o.confidence, o.assumed) == (d.ESCAPED, "low", True)
        assert d.cell(bug()) == "🏭 post-release·L (assumed)"

    @pytest.mark.parametrize("pr", [
        bug(labels=["hotfix"]), bug(title="Hotfix: crash on start"), bug(branch="hotfix/crash"), bug(labels=[{"name": "incident"}]),
        {"pr_type": "feature", "title": 'Revert "add export"'},
    ])
    def test_strong_post_release_evidence_is_high_confidence(self, pr):
        o = d.classify(pr)
        assert (o.kind, o.confidence, o.assumed) == (d.ESCAPED, "high", False)

    def test_medium_evidence_is_medium_confidence(self):
        assert d.classify(bug(title="fix regression in export")).confidence == "medium"

    def test_a_fix_aimed_at_an_unreleased_feature_branch_is_pre_release(self):
        o = d.classify(bug(target_branch="feature/export"))
        assert (o.kind, o.confidence) == (d.PRE_RELEASE, "high") and "target" in o.signals[0]

    @pytest.mark.parametrize("text", ["found in QA: wrong total", "address review comments", "caught by CI before release", "pre-release crash"])
    def test_pre_release_wording_in_the_title_or_description(self, text):
        assert d.classify(bug(title=text)).kind == d.PRE_RELEASE
        assert d.classify(bug(description=text)).kind == d.PRE_RELEASE
        assert d.classify(bug(jira_summary=text)).kind == d.PRE_RELEASE

    def test_conflicting_evidence_is_low_confidence_and_goes_to_escaped(self):
        o = d.classify(bug(title="found in QA", labels=["customer"]))
        assert (o.kind, o.confidence, o.assumed) == (d.ESCAPED, "low", False)

    def test_a_weak_hint_alone_is_still_assumed(self):
        o = d.classify(bug(target_branch="main"))
        assert (o.kind, o.confidence, o.assumed) == (d.ESCAPED, "low", True)

    def test_security_fixes_are_defects_too(self):
        assert d.classify({"pr_type": "security", "title": "patch xss", "labels": ["incident"]}).confidence == "high"


class TestSummary:
    def test_counts_by_origin_and_confidence(self):
        prs = [bug(), bug(labels=["hotfix"]), bug(title="fix regression"), bug(target_branch="feature/x"), {"pr_type": "feature", "title": "x"}]
        assert d.summarize(prs) == {"defects": 4, "escaped": 3, "pre_release": 1, "high": 1, "medium": 1, "low": 1, "assumed": 1}


class TestInReports:
    def test_the_dashboard_separates_escaped_from_pre_release_and_says_what_is_assumed(self):
        prs = [{**bug(), "state": "open"}, {**bug(labels=["hotfix"]), "state": "open"}, {**bug(target_branch="feature/x"), "state": "open"}, {"pr_type": "feature", "state": "open"}]
        out = build_metrics_dashboard(prs)
        assert "| Defect Rate (proxy) | 75.0% (3/4) |" in out
        assert "| Escaped Defects (post-release) | 2/3 defect PRs (50.0% of 4) |" in out and "1 backed by evidence" in out and "1 assumed" in out
        assert "| Pre-release Fixes | 1 |" in out

    def test_change_failure_rate_counts_only_escaped_merged_defects_and_shows_the_evidence(self):
        merged = {"state": "merged", "merged_at": "2026-01-0{}T10:00:00Z", "created_at": "2026-01-01T00:00:00Z"}
        prs = [{**merged, "merged_at": f"2026-01-0{i}T10:00:00Z", **p} for i, p in enumerate([
            bug(labels=["hotfix"]), bug(target_branch="feature/x"), {"pr_type": "feature"}, {"pr_type": "feature"}, bug()], 2)]
        out = build_metrics_dashboard(prs)
        row = [l for l in out.splitlines() if l.startswith("| Change Failure Rate")][0]
        assert "40.0% (2/5; 1 with evidence, 1 assumed)" in row   # the pre-release fix is not a failure

    def test_per_pr_column_and_single_pr_row(self):
        assert REGISTRY["origin"].cell(bug(labels=["hotfix"]), {}) == "🏭 post-release·H"
        rows = build_single_pr_metrics(bug(labels=["hotfix"]), ("origin",))
        assert "Defect Origin" in rows[2] and "label: hotfix" in rows[2]
        assert len(build_single_pr_metrics({"pr_type": "feature"}, ("origin",))) == 2   # header only: no row for a non-defect


class TestRealisticWording:
    """Wording that looks like evidence but is not (reviewed false positives)."""

    @pytest.mark.parametrize("pr", [
        bug(title="Fix comment parser crash"), bug(title="Fix review comments posting to GitHub"),
        bug(description="updates the CHANGELOG Unreleased section"), bug(labels=["internal-tools"]), bug(labels=["staging-env"]),
        bug(title="Fix build artifact upload"), bug(branch="patch-1"), bug(description="- [ ] Hotfix\n- [ ] addressed review comments"),
        bug(title="Fix privilege escalation"), bug(labels=["unsupported-platform"]), bug(description="adds rollback on failure"),
    ])
    def test_these_stay_post_release_by_default(self, pr):
        o = d.classify(pr)
        assert o.kind == d.ESCAPED and not (o.confidence == "high" and not o.assumed)

    @pytest.mark.parametrize("title", ["Rollback feature flag docs", "Reverting to the legacy parser as an option", "Revert to legacy parser option"])
    def test_a_title_that_only_starts_like_a_revert_is_not_one(self, title):
        assert d.classify({"pr_type": "feature", "title": title}) is None

    @pytest.mark.parametrize("title", ['Revert "add export"', "Revert #12", "Revert 1a2b3c4d", "Rollback of PR #12", "Revert pull request 7"])
    def test_real_reverts_are_defects_counted_once(self, title):
        o = d.classify({"pr_type": "feature", "title": title})
        assert (o.kind, o.confidence) == (d.ESCAPED, "high") and o.signals.count("revert") == 1

    def test_labels_match_as_whole_names_including_scoped_ones(self):
        assert d.classify(bug(labels=["priority/p1"])).confidence == "medium"
        assert d.classify(bug(labels=[{"name": "Found In QA"}])).kind == d.PRE_RELEASE
        assert d.classify(bug(labels=[None, {"name": None}, ""])).assumed

    def test_a_ticked_option_still_counts(self):
        assert d.classify(bug(description="- [x] Hotfix")).confidence == "high"

    def test_the_defect_rate_counts_the_same_prs_the_origin_rows_split(self):
        prs = [{"state": "open", "pr_type": "feature", "title": 'Revert "add x"'}, {"state": "open", "pr_type": "bug", "title": "fix"}, {"state": "open", "pr_type": "feature", "title": "y"}]
        out = build_metrics_dashboard(prs)
        assert "| Defect Rate (proxy) | 66.7% (2/3) |" in out and "Escaped Defects (post-release) | 2/2 defect PRs" in out
