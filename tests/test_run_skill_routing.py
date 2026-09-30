"""Tests for run_skill intent detection and routing logic.

Covers the core routing bug: _detect_intent must be called AFTER _KNOWN_SKILLS is
populated by ensure_ygs_skills(), otherwise PR review URLs fall back to ygs-ask.
"""
import sys
import pytest

import scripts.adhoc.run_skill as run_skill_mod
from scripts.adhoc.run_skill import _detect_intent, _best_review_skill, _REVIEW_SKILL_PREFERENCE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _with_known_skills(*skills: str):
    """Context: temporarily populate _KNOWN_SKILLS with the given skill names."""
    import scripts.common.claude_runner as cr
    orig = set(cr._KNOWN_SKILLS)
    cr._KNOWN_SKILLS.update(skills)
    try:
        yield
    finally:
        cr._KNOWN_SKILLS.clear()
        cr._KNOWN_SKILLS.update(orig)


from contextlib import contextmanager

@contextmanager
def known_skills(*skills: str):
    import scripts.common.claude_runner as cr
    orig = set(cr._KNOWN_SKILLS)
    cr._KNOWN_SKILLS.clear()   # replace entirely — don't let prior-test installs bleed in
    cr._KNOWN_SKILLS.update(skills)
    try:
        yield
    finally:
        cr._KNOWN_SKILLS.clear()
        cr._KNOWN_SKILLS.update(orig)


# ---------------------------------------------------------------------------
# _best_review_skill
# ---------------------------------------------------------------------------

class TestBestReviewSkill:
    def test_empty_known_skills_returns_none(self):
        with known_skills():
            result = _best_review_skill(deep=True)
        assert result is None

    def test_deep_prefers_review_deep_when_installed(self):
        with known_skills("ygs-review-deep", "ygs-review-pr"):
            result = _best_review_skill(deep=True)
        assert result == "ygs-review-deep"

    def test_non_deep_skips_review_deep(self):
        with known_skills("ygs-review-deep", "ygs-review-pr"):
            result = _best_review_skill(deep=False)
        assert result == "ygs-review-pr"

    def test_falls_back_to_review_pr_when_deep_missing(self):
        with known_skills("ygs-review-pr"):
            result = _best_review_skill(deep=True)
        assert result == "ygs-review-pr"

    def test_falls_back_to_any_review_skill(self):
        with known_skills("ygs-code-review"):
            result = _best_review_skill(deep=True)
        assert result == "ygs-code-review"

    def test_non_review_skills_ignored(self):
        with known_skills("ygs-standup", "ygs-ask"):
            result = _best_review_skill(deep=True)
        assert result is None


# ---------------------------------------------------------------------------
# _detect_intent
# ---------------------------------------------------------------------------

BB_PR_URL = "https://bitbucket.org/example-org/example-repo/pull-requests/49058/overview"
GH_PR_URL = "https://github.com/bhatti/formicary/pull/123"

class TestDetectIntent:
    def test_non_ask_skill_unchanged_regardless_of_url(self):
        with known_skills("ygs-review-deep"):
            result = _detect_intent(f"deep review {BB_PR_URL}", "ygs-review-pr")
        assert result == "ygs-review-pr"

    def test_no_url_returns_original_skill(self):
        with known_skills("ygs-review-deep"):
            result = _detect_intent("summarize the sprint standup", "ygs-ask")
        assert result == "ygs-ask"

    def test_bb_pr_url_routes_to_review_deep_when_deep_requested(self):
        with known_skills("ygs-review-deep", "ygs-review-pr"):
            result = _detect_intent(f"deep review {BB_PR_URL}", "ygs-ask")
        assert result == "ygs-review-deep"

    def test_gh_pr_url_routes_to_review_deep_when_deep_requested(self):
        with known_skills("ygs-review-deep", "ygs-review-pr"):
            result = _detect_intent(f"deep review {GH_PR_URL}", "ygs-ask")
        assert result == "ygs-review-deep"

    def test_pr_url_without_deep_routes_to_review_pr(self):
        with known_skills("ygs-review-deep", "ygs-review-pr"):
            result = _detect_intent(f"review {BB_PR_URL}", "ygs-ask")
        assert result == "ygs-review-pr"

    def test_pr_url_falls_back_to_ask_when_no_review_skills_installed(self):
        """The critical regression: if skills not loaded, falls back to ygs-ask."""
        with known_skills():  # empty — simulates calling _detect_intent before ensure_ygs_skills()
            result = _detect_intent(f"deep review {BB_PR_URL}", "ygs-ask")
        assert result == "ygs-ask"

    def test_pr_url_routes_correctly_when_only_review_pr_installed(self):
        with known_skills("ygs-review-pr"):
            result = _detect_intent(f"deep review {BB_PR_URL}", "ygs-ask")
        assert result == "ygs-review-pr"

    def test_deep_keyword_case_insensitive(self):
        with known_skills("ygs-review-deep"):
            result = _detect_intent(f"DEEP review {BB_PR_URL}", "ygs-ask")
        assert result == "ygs-review-deep"

    def test_pull_request_url_pattern_matches(self):
        urls = [
            "https://bitbucket.org/org/repo/pull-requests/123",
            "https://github.com/org/repo/pull/456",
            "https://gitlab.com/org/repo/merge_request/789",
        ]
        with known_skills("ygs-review-pr"):
            for url in urls:
                assert _detect_intent(f"review {url}", "ygs-ask") == "ygs-review-pr", f"failed for {url}"


# ---------------------------------------------------------------------------
# upload_html_report: post_fallback=False suppresses duplicate link
# ---------------------------------------------------------------------------

class TestUploadHtmlReportPostFallback:
    """Verify that post_fallback=False prevents the fallback text link from being posted."""

    def _setup_stubs(self, monkeypatch):
        import scripts.standup.slack_client as sc
        import scripts.common.slack_format as sf

        posted: list[str] = []

        monkeypatch.setattr(sc, "upload_file", lambda *a, **kw: False)
        monkeypatch.setattr(sc, "_post_message_ts", lambda config, text, **kw: posted.append(text))
        monkeypatch.setattr(sf, "build_artifact_links",
                            lambda *a, **kw: ("http://example.com/report.html", "http://example.com/artifacts"))
        return posted

    def test_post_fallback_false_suppresses_link_on_upload_failure(self, monkeypatch):
        from scripts.standup.slack_client import upload_html_report
        posted = self._setup_stubs(monkeypatch)

        result = upload_html_report(
            config={},
            html_content="<html>report</html>",
            filename="report.html",
            thread_ts="123.456",
            task_type="run",
            post_fallback=False,
        )

        assert posted == [], "fallback link must not be posted when post_fallback=False"
        assert result is False  # upload failed, no fallback posted

    def test_post_fallback_true_posts_link_on_upload_failure(self, monkeypatch):
        from scripts.standup.slack_client import upload_html_report
        posted = self._setup_stubs(monkeypatch)

        result = upload_html_report(
            config={},
            html_content="<html>report</html>",
            filename="report.html",
            thread_ts="123.456",
            task_type="run",
            post_fallback=True,
        )

        assert len(posted) == 1, "fallback link must be posted when post_fallback=True"
        assert "report.html" in posted[0]
        assert result is True
