"""Tests for scripts/analyze/audit_options.py (run options carried across task pods)."""

import json

from scripts.analyze.audit_options import AuditOptions, load, load_unmatched, parse_reviewers, resolve, write


class TestParseReviewers:
    def test_splits_trims_dedupes_case_insensitively(self):
        assert parse_reviewers(" Alice, bob ,alice,,") == ("Alice", "bob")

    def test_empty_and_template_placeholder(self):
        assert parse_reviewers("") == ()
        assert parse_reviewers("<no value>") == ()
        assert parse_reviewers(None) == ()


class TestResolve:
    def test_defaults(self):
        assert resolve({}, {}) == AuditOptions()

    def test_slack_flag_wins_over_env(self):
        opts = resolve({"PR_AUDIT_REVIEWERS": "env1"}, {"reviewers": "a,b", "dry_run": True})
        assert opts.reviewers == ("a", "b")
        assert opts.dry_run is True

    def test_env_used_when_no_flag(self):
        opts = resolve({"PR_AUDIT_DRY_RUN": "true", "PR_AUDIT_REVIEWERS": "x"}, {})
        assert opts.dry_run is True
        assert opts.reviewers == ("x",)

    def test_falsey_env_values(self):
        for v in ("", "0", "false", "no", "<no value>"):
            assert resolve({"PR_AUDIT_DRY_RUN": v}, {}).dry_run is False


class TestWriteLoad:
    def test_round_trip(self, tmp_path):
        write(tmp_path, AuditOptions(dry_run=True, reviewers=("a", "b")), unmatched=["typo"])
        assert json.loads((tmp_path / "run_options.json").read_text())["dry_run"] is True
        assert load(tmp_path, {}) == AuditOptions(dry_run=True, reviewers=("a", "b"))
        assert load_unmatched(tmp_path) == ["typo"]

    def test_missing_file_falls_back_to_env(self, tmp_path):
        opts = load(tmp_path, {"PR_AUDIT_DRY_RUN": "1", "PR_AUDIT_REVIEWERS": "z"})
        assert opts.dry_run is True and opts.reviewers == ("z",)

    def test_env_dry_run_is_sticky_even_if_file_says_false(self, tmp_path):
        write(tmp_path, AuditOptions())
        assert load(tmp_path, {"PR_AUDIT_DRY_RUN": "1"}).dry_run is True

    def test_corrupt_file_falls_back_to_env(self, tmp_path):
        (tmp_path / "run_options.json").write_text("{nope")
        assert load(tmp_path, {}) == AuditOptions()


def test_load_unmatched_missing_or_corrupt(tmp_path):
    assert load_unmatched(tmp_path) == []
    (tmp_path / "run_options.json").write_text("{bad")
    assert load_unmatched(tmp_path) == []


def test_reviewer_names_are_requested_names_plus_what_they_matched(tmp_path):
    from scripts.analyze.audit_options import reviewer_names
    assert reviewer_names(tmp_path) == ()
    write(tmp_path, AuditOptions(reviewers=("CJ", "pat")), aliases={"CJ": ["Casey Jordan", "cjordan"]})
    assert set(reviewer_names(tmp_path)) == {"CJ", "pat", "Casey Jordan", "cjordan"}
    write(tmp_path, AuditOptions(), unmatched=["CJ"])  # fallback: nobody is a priority reviewer
    assert reviewer_names(tmp_path) == ()


def test_aliases_round_trip(tmp_path):
    from scripts.analyze.audit_options import load_aliases
    write(tmp_path, AuditOptions(reviewers=("cj",)), aliases={"cj": ["Casey Jordan", "cj"]})
    assert load_aliases(tmp_path) == {"cj": ["Casey Jordan", "cj"]}
    assert load_aliases(tmp_path / "missing") == {}
