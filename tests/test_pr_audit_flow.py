"""End-to-end flow of the PR audit pipeline, with and without --reviewers: audit-prs -> create-skill-pr.

Real code from the PR fetch (GitHub, via a fake `gh`) through reports, learnings restriction and the knowledge-base
files; only the network/LLM edges are faked (`gh`, the model, git push, Slack).
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scripts.analyze import create_skill_pr, run_pr_audit

LEARNINGS = {
    "entries": [
        {"category": "security", "rule": "Check tenant on every query", "why": "cross-tenant leak", "look_for": "queries without tenant",
         "pr_count": 2, "prs": ["#1", "#2"], "reviewers": ["alice"]},
        {"category": "testing", "rule": "No sleeps in tests", "why": "flaky", "look_for": "time.sleep in tests",
         "pr_count": 2, "prs": ["#1", "#2"], "reviewers": ["bob"]},
    ],
    "reviewer_profiles": [
        {"reviewer": "alice", "focus_areas": [{"category": "security", "count": 3}], "signature_concerns": ["tenant isolation"], "quotes": []},
        {"reviewer": "bob", "focus_areas": [{"category": "testing", "count": 3}], "signature_concerns": ["flaky tests"], "quotes": []},
    ],
}


def _row(num, reviewer, author="dev"):
    return {"number": num, "title": f"t{num}", "state": "MERGED", "body": "", "author": {"login": author},
            "createdAt": "2026-01-01T00:00:00Z", "mergedAt": "2026-01-02T00:00:00Z", "url": f"https://github.com/o/r/pull/{num}",
            "headRefName": "b", "baseRefName": "main", "labels": [], "files": [{"path": "a.py", "additions": 3, "deletions": 1}],
            "comments": [], "reviewDecision": "",
            "reviews": [{"author": {"login": reviewer}, "state": "COMMENTED", "body": f"please change {num}: it breaks the lock ordering"}]}


class FakeGh:
    """`gh pr list` answers: a search for a login returns that login's PRs; without a search, every PR."""

    def __init__(self):
        self.all = [_row(1, "alice"), _row(2, "bob"), _row(3, "alice")]
        self.cmds = []

    def __call__(self, cmd, **kw):
        if cmd[0] != "gh":  # e.g. `git remote get-url` run by the audit
            return MagicMock(returncode=1, stdout="", stderr="")
        self.cmds.append(cmd)
        if cmd[cmd.index("--state") + 1] != "merged":
            return MagicMock(returncode=0, stdout="[]", stderr="")
        if "--search" in cmd:
            who = cmd[cmd.index("--search") + 1].split(":")[1].split(" ")[0]
            rows = [r for r in self.all if any(rv["author"]["login"] == who for rv in r["reviews"])]
        else:
            rows = self.all
        return MagicMock(returncode=0, stdout=json.dumps(rows), stderr="")


def _fake_claude(reports_dir: Path, prompts: list, learnings: dict, model_changelog=None):
    def run(prompt, **kw):
        prompts.append(prompt)
        (reports_dir / "pr_audit_report.md").write_text("# PR Audit -- o/r\n\n### Executive Summary\n\n" + "Review depth is shallow. " * 20)
        (reports_dir / "pr_audit_findings.json").write_text(json.dumps({"findings": [], "metrics": {}}))
        (reports_dir / "skill_improvements.json").write_text(json.dumps({
            "repo_skill_changes": [{"action": "create", "file_path": ".claude/skills/x/SKILL.md", "description": "d", "full_content": "body"}],
            "new_docs": [], "ygs_recommendations": []}))
        (reports_dir / "review_learnings.json").write_text(json.dumps(learnings))
        if model_changelog is not None:
            (reports_dir / "changelog_model.json").write_text(json.dumps(model_changelog))
        return SimpleNamespace(status="DONE", output="", status_json={"status": "DONE", "spec_gap_count": 0, "design_gap_count": 0,
                                                                         "skill_gap_count": 0, "practice_gap_count": 0})
    return run


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for k in ("PR_AUDIT_REVIEWERS", "PR_AUDIT_DRY_RUN", "PR_URLS", "JIRA_BOARDS", "JIRA_SPACE", "PR_AUDIT_FILTER", "FORMICARY_PUBLIC_URL", "JOB_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("DEFAULT_TRACKER", "github")
    monkeypatch.setenv("GH_ORG", "o")
    monkeypatch.setenv("GH_REPO", "r")
    monkeypatch.setenv("PR_SIGNALS", "off")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("ANTHROPIC_BEDROCK_BASE_URL", "http://x")
    return tmp_path


def run_audit(workspace, monkeypatch, message, gh=None, learnings=None, bb=None, codeowners="", model_changelog=None, diff=None):
    """Run audit-prs the way the pod does: the Slack text arrives in SLACK_MESSAGE."""
    monkeypatch.setenv("SLACK_MESSAGE", message)
    gh = gh or FakeGh()
    learnings = learnings or LEARNINGS
    prompts: list = []
    reports = workspace / "reports"

    def clone(url, branch, dest, depth):
        (dest / ".git").mkdir(parents=True, exist_ok=True)
        if codeowners:
            (dest / ".github").mkdir(exist_ok=True)
            (dest / ".github" / "CODEOWNERS").write_text(codeowners)
        return True, "main"

    diff_calls: list = []
    fetch_diff = lambda config, tracker, number, max_bytes=0: (diff_calls.append(number) or (diff or "", "" if diff else "no diff in this test"))

    with patch("scripts.analyze.pr_fetcher.subprocess.run", side_effect=gh), \
         patch("scripts.analyze.pr_fetcher._fetch_gh_review_comments", return_value=[]), \
         patch("scripts.analyze.run_pr_audit.subprocess.run", side_effect=gh), \
         patch("scripts.analyze.run_pr_audit.clone_for_audit", side_effect=clone), \
         patch("scripts.analyze.run_pr_audit.ensure_ygs_skills"), \
         patch("scripts.analyze.run_pr_audit.validate_claude_config"), \
         patch("scripts.analyze.run_pr_audit.apply_project_skills", return_value=0), \
         patch("scripts.analyze.run_pr_audit._attach_review_signals"), \
         patch("scripts.analyze.run_pr_audit._load_skill_md", return_value="SKILL"), \
         patch("scripts.analyze.run_pr_audit.link_pr_to_issue", return_value=None), \
         patch("scripts.analyze.run_pr_audit.run_claude", side_effect=_fake_claude(reports, prompts, learnings, model_changelog)), \
         patch("scripts.analyze.change_impact.fetch_pr_diff", side_effect=fetch_diff), \
         patch("requests.get", side_effect=bb or (lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected HTTP call")))):
        try:
            run_pr_audit.main.main(args=[], standalone_mode=False)
            code = 0
        except SystemExit as e:
            code = e.code or 0
    return SimpleNamespace(code=code, gh=gh, prompts=prompts, reports=reports, diff_calls=diff_calls)


def run_create(workspace, monkeypatch, push=False):
    repo = workspace / "repo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEBASE_DIR", str(repo))
    with patch.object(create_skill_pr, "_run_git", return_value=MagicMock(returncode=0, stdout="")), \
         patch.object(create_skill_pr, "configure_git"), \
         patch.object(create_skill_pr, "post_message"), \
         patch.object(create_skill_pr, "push_branch") as pushed, \
         patch.object(create_skill_pr, "_create_pr", return_value={"url": "https://x/pull/1", "number": 1}) as created:
        create_skill_pr.main()
    return SimpleNamespace(pushed=pushed, created=created, repo=repo)


def kb_files(workspace):
    return sorted(str(p.relative_to(workspace / "reports/dry_run/files")) for p in (workspace / "reports/dry_run/files").rglob("*.md")) \
        if (workspace / "reports/dry_run/files").exists() else []


class TestWithoutReviewers:
    def test_audit_then_pr_includes_everyones_learnings(self, workspace, monkeypatch):
        audit = run_audit(workspace, monkeypatch, "https://github.com/o/r --n-prs 10")
        assert audit.code == 0
        assert all("--search" not in c for c in audit.gh.cmds)  # the plain listing, as before this feature
        md = (audit.reports / "review_learnings.md").read_text()
        assert "Check tenant on every query" in md and "No sleeps in tests" in md
        assert "Reviewer focus" not in audit.prompts[0]
        assert (audit.reports / "review_learnings.html").exists() and (audit.reports / "pr_audit_report.html").exists()

        pr = run_create(workspace, monkeypatch)
        pr.pushed.assert_called_once()
        pr.created.assert_called_once()
        written = {p.relative_to(pr.repo).as_posix() for p in pr.repo.rglob("*.md") if ".claude" in p.parts}
        assert ".claude/skills/review-knowledge/checklists/security.md" in written
        assert ".claude/skills/review-knowledge/checklists/testing.md" in written
        assert not any("/reviewers/" in w for w in written)  # profiles only exist for requested reviewers
        report = (audit.reports / "skill_updates_report.md").read_text()
        assert "https://x/pull/1" in report and "review-knowledge" in report
        assert (audit.reports / "skill_updates_report.html").exists()


class TestWithReviewers:
    def test_dry_run_only_requested_reviewers_end_to_end(self, workspace, monkeypatch):
        audit = run_audit(workspace, monkeypatch, "https://github.com/o/r --n-prs 10 --reviewers alice --dry-run")
        assert audit.code == 0
        searches = [c[c.index("--search") + 1] for c in audit.gh.cmds if "--search" in c]
        assert "reviewed-by:alice -author:alice" in searches and not any("bob" in s for s in searches)
        assert "Reviewer focus" in audit.prompts[0] and "ONLY" in audit.prompts[0]
        ctx = json.loads((audit.reports / "run_options.json").read_text())
        assert ctx["dry_run"] is True and ctx["reviewers"] == ["alice"] and ctx["reviewer_aliases"]["alice"] == ["alice"]

        md = (audit.reports / "review_learnings.md").read_text()
        assert "Check tenant on every query" in md and "No sleeps in tests" not in md  # bob's rule is dropped
        assert "What alice looks for" in md and "What bob looks for" not in md

        pr = run_create(workspace, monkeypatch)
        pr.pushed.assert_not_called()
        pr.created.assert_not_called()
        files = kb_files(workspace)
        assert ".claude/skills/review-knowledge/checklists/security.md" in files
        assert ".claude/skills/review-knowledge/checklists/testing.md" not in files
        assert ".claude/skills/review-knowledge/reviewers/alice.md" in files
        assert not any("bob" in f for f in files)
        assert "Dry run" in (audit.reports / "skill_updates_report.md").read_text()
        assert json.loads((workspace / "pr.json").read_text())["number"] == 0

    def test_reviewers_without_dry_run_creates_the_pr(self, workspace, monkeypatch):
        run_audit(workspace, monkeypatch, "https://github.com/o/r --reviewers alice")
        pr = run_create(workspace, monkeypatch)
        pr.pushed.assert_called_once()
        pr.created.assert_called_once()
        assert (pr.repo / ".claude/skills/review-knowledge/reviewers/alice.md").exists()
        assert not (pr.repo / ".claude/skills/review-knowledge/checklists/testing.md").exists()

    def test_no_match_audits_everyone_and_says_so(self, workspace, monkeypatch):
        audit = run_audit(workspace, monkeypatch, "https://github.com/o/r --reviewers nobody --dry-run")
        assert audit.code == 0
        assert audit.gh.cmds[-1].count("--search") == 0  # the second, unfiltered listing produced the PRs
        assert "Reviewer focus" not in audit.prompts[0] and "could not be matched" in audit.prompts[0]
        md = (audit.reports / "review_learnings.md").read_text()
        assert "None of the requested reviewers (nobody)" in md and "all reviewers are shown" in md
        assert "Check tenant on every query" in md and "No sleeps in tests" in md  # everyone's learnings
        ctx = json.loads((audit.reports / "run_options.json").read_text())
        assert ctx["reviewers"] == [] and ctx["unmatched_reviewers"] == ["nobody"]
        run_create(workspace, monkeypatch)
        files = kb_files(workspace)
        assert ".claude/skills/review-knowledge/checklists/testing.md" in files and not any("/reviewers/" in f for f in files)

    def test_partial_match_runs_and_reports_the_missing_name(self, workspace, monkeypatch):
        audit = run_audit(workspace, monkeypatch, "https://github.com/o/r --reviewers alice,typo")
        assert audit.code == 0
        assert "typo" in (audit.reports / "review_learnings.md").read_text()
        assert json.loads((audit.reports / "run_options.json").read_text())["unmatched_reviewers"] == ["typo"]


# -- Bitbucket (the Jira/BB workflow): same pipeline, different fetch --------------------------------------

BB_LEARNINGS = {
    "entries": [
        {"category": "security", "rule": "Check tenant on every query", "why": "leak", "look_for": "queries", "pr_count": 1,
         "prs": ["#1"], "reviewers": ["Alice Smith"]},
        {"category": "testing", "rule": "No sleeps in tests", "why": "flaky", "look_for": "sleep", "pr_count": 2,
         "prs": ["#2"], "reviewers": ["Bob Lee"]},
    ],
    "reviewer_profiles": [
        {"reviewer": "Alice Smith", "focus_areas": [{"category": "security", "count": 3}], "signature_concerns": ["tenant isolation"], "quotes": []},
        {"reviewer": "Bob Lee", "focus_areas": [{"category": "testing", "count": 3}], "signature_concerns": ["flaky tests"], "quotes": []},
    ],
}
PEOPLE = {"asmith": ("Alice Smith", "asmith", "{u-a}", "acc-a"), "blee": ("Bob Lee", "blee", "{u-b}", "acc-b")}


def _bb_user(key):
    d, n, u, a = PEOPLE[key]
    return {"display_name": d, "nickname": n, "uuid": u, "account_id": a}


def _bb_pr(pid, reviewer_key):
    return {"id": pid, "title": f"t{pid}", "state": "MERGED", "updated_on": "2026-01-02T00:00:00Z", "created_on": "2026-01-01T00:00:00Z",
            "author": {"display_name": "Dev", "nickname": "dev"}, "description": "", "source": {"branch": {"name": "b"}},
            "destination": {"branch": {"name": "main"}}, "links": {"html": {"href": f"https://bb/pr/{pid}"}},
            "reviewers": [_bb_user(reviewer_key)],
            "participants": [{"user": _bb_user(reviewer_key), "role": "REVIEWER", "approved": False},
                             {"user": {"display_name": "Dev", "nickname": "dev"}, "role": "AUTHOR"}]}


class FakeBitbucket:
    PRS = {1: _bb_pr(1, "asmith"), 2: _bb_pr(2, "blee"), 3: _bb_pr(3, "asmith")}
    OWNER = {1: "asmith", 2: "blee", 3: "asmith"}

    def __init__(self):
        self.list_queries, self.detail_ids = [], set()

    def __call__(self, url, auth=None, timeout=None):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(url)
        resp = MagicMock(status_code=200)
        if parsed.path.endswith("/members"):
            resp.json.return_value = {"values": [{"user": _bb_user(k)} for k in PEOPLE], "next": ""}
        elif parsed.path.endswith("/pullrequests"):
            q = parse_qs(parsed.query).get("q", [""])[0]
            self.list_queries.append(q)
            rows = list(self.PRS.values())
            for key, (_, nick, _, acct) in PEOPLE.items():
                if q in (f'reviewers.nickname="{nick}"', f'reviewers.account_id="{acct}"'):
                    rows = [r for r in rows if self.OWNER[r["id"]] == key]
                    break
            else:
                if q and q != "comment_count>0":
                    rows = []
            resp.json.return_value = {"values": rows, "next": ""}
        else:
            pid = int(parsed.path.split("/pullrequests/")[1].split("/")[0])
            self.detail_ids.add(pid)
            if parsed.path.endswith("/comments"):
                resp.json.return_value = {"values": [{"user": _bb_user(self.OWNER[pid]), "content": {"raw": f"PR {pid}: hold the lock in order"}}], "next": ""}
            else:
                resp.json.return_value = {"values": [{"new": {"path": "a.py"}, "lines_added": 3, "lines_removed": 1}], "next": ""}
        return resp


@pytest.fixture
def bb_workspace(workspace, monkeypatch):
    monkeypatch.setenv("DEFAULT_TRACKER", "jira")
    monkeypatch.delenv("GH_ORG", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.setenv("BITBUCKET_WORKSPACE", "ws")
    monkeypatch.setenv("BITBUCKET_REPO", "repo")
    monkeypatch.setenv("BITBUCKET_API_BASE", "https://api.bitbucket.org/2.0")
    return workspace


def run_bb_audit(workspace, monkeypatch, message, bb):
    return run_audit(workspace, monkeypatch, message, learnings=BB_LEARNINGS, bb=bb)


class TestBitbucketWithoutReviewers:
    def test_all_prs_and_everyones_learnings(self, bb_workspace, monkeypatch):
        bb = FakeBitbucket()
        audit = run_bb_audit(bb_workspace, monkeypatch, "--n-prs 10", bb)
        assert audit.code == 0
        assert bb.list_queries == [""] and bb.detail_ids == {1, 2, 3}
        md = (audit.reports / "review_learnings.md").read_text()
        assert "Check tenant on every query" in md and "No sleeps in tests" in md
        pr = run_create(bb_workspace, monkeypatch)
        pr.created.assert_called_once()
        assert not any("/reviewers/" in str(p) for p in pr.repo.rglob("*.md"))


class TestBitbucketWithReviewers:
    def test_nickname_filters_server_side_and_report_has_only_that_person(self, bb_workspace, monkeypatch):
        bb = FakeBitbucket()
        audit = run_bb_audit(bb_workspace, monkeypatch, "--n-prs 2 --reviewers asmith --dry-run", bb)
        assert audit.code == 0
        assert bb.list_queries == ['reviewers.nickname="asmith"']  # --n-prs 2 is satisfied server-side: no scan
        assert 2 not in bb.detail_ids  # Bob's PR is never enriched
        assert "asmith = " in audit.prompts[0] and "Alice Smith" in audit.prompts[0]
        md = (audit.reports / "review_learnings.md").read_text()
        assert "Check tenant on every query" in md and "No sleeps in tests" not in md and "Bob Lee" not in md
        run_create(bb_workspace, monkeypatch)
        files = kb_files(bb_workspace)
        # one PR only, written under her display name: kept because she is a requested reviewer
        assert ".claude/skills/review-knowledge/checklists/security.md" in files
        assert ".claude/skills/review-knowledge/reviewers/alice-smith.md" in files
        assert ".claude/skills/review-knowledge/checklists/testing.md" not in files
        assert not any("bob" in f for f in files)

    def test_display_name_works_through_the_commented_pr_scan(self, bb_workspace, monkeypatch):
        bb = FakeBitbucket()
        audit = run_bb_audit(bb_workspace, monkeypatch, '--reviewers "Bob Lee" --dry-run', bb)
        assert audit.code == 0 and 1 not in bb.detail_ids and 2 in bb.detail_ids
        assert "No sleeps in tests" in (audit.reports / "review_learnings.md").read_text()

    def test_initials_first_name_and_accents_resolve_through_the_workspace_directory(self, bb_workspace, monkeypatch):
        bb = FakeBitbucket()
        audit = run_bb_audit(bb_workspace, monkeypatch, "--n-prs 5 --reviewers AS,blee --dry-run", bb)
        assert audit.code == 0
        md = (audit.reports / "review_learnings.md").read_text()
        assert "AS → Alice Smith" in md or "AS → Alice Smith, asmith" in md
        assert "Check tenant on every query" in md and "No sleeps in tests" in md  # both people were found

    def test_unknown_name_audits_everyone_and_says_so(self, bb_workspace, monkeypatch):
        bb = FakeBitbucket()
        audit = run_bb_audit(bb_workspace, monkeypatch, "--reviewers CJ --dry-run", bb)
        assert audit.code == 0 and audit.prompts
        md = (audit.reports / "review_learnings.md").read_text()
        assert "None of the requested reviewers (CJ)" in md
        assert "Check tenant on every query" in md and "No sleeps in tests" in md
        assert bb.detail_ids == {1, 2, 3}  # fell back to the plain latest PRs


# -- Changelog & Blast Radius ----------------------------------------------------------------------------------------

CODEOWNERS_TEXT = "* @org/everyone\n/proto/ @org/platform\n/web/ @org/frontend\n/auth/ @org/identity\n"
PROTO_DIFF = ("diff --git a/proto/pay.proto b/proto/pay.proto\n--- a/proto/pay.proto\n+++ b/proto/pay.proto\n@@ -1,3 +1,2 @@\n"
              "-  string currency = 3;\n   int64 amount = 1;\n")


def _gh_with(rows):
    gh = FakeGh()
    gh.all = rows
    return gh


def _row_files(num, files, title=None, reviewer="alice"):
    row = _row(num, reviewer)
    row["files"] = [{"path": p, "additions": a, "deletions": d} for p, a, d in files]
    if title:
        row["title"] = title
    return row


class TestChangelogGitHub:
    ROWS = [_row_files(10, [("proto/pay.proto", 0, 1)], "Drop currency from Pay"), _row_files(11, [("auth/token.py", 2, 1)], "Tighten token expiry"),
            _row_files(12, [("web/button.ts", 2, 1)], "Button colour")]

    def test_section_files_teams_blast_and_breaking_candidate(self, workspace, monkeypatch):
        model = {"features": [{"title": "Pay API trimmed", "summary": "Removes the currency field", "prs": [10], "category": "platform"}]}
        audit = run_audit(workspace, monkeypatch, "https://github.com/o/r --n-prs 10", gh=_gh_with(self.ROWS), codeowners=CODEOWNERS_TEXT,
                          model_changelog=model, diff=PROTO_DIFF)
        assert audit.code == 0
        report = (audit.reports / "pr_audit_report.md").read_text()
        assert report.count("## Changelog & Blast Radius") == 1
        out = json.loads((audit.reports / "changelog.json").read_text())
        pay = next(f for f in out["features"] if f["title"] == "Pay API trimmed")
        assert pay["teams"] == ["platform"] and pay["blast"]["external"] is True
        assert [(b["kind"], b["confidence"]) for b in pay["breaking"]] == [("field_removed", "high")]
        token = next(f for f in out["features"] if 11 in f["prs"])
        assert token["teams"] == ["identity"] and token["blast"]["level"] == "high"  # auth is always high
        assert all(12 not in f["prs"] for f in out["features"])  # a low-blast button change is not a notable change
        for name in ("changelog.md", "changelog.html", "change_facts.json"):
            assert (audit.reports / name).exists()
        assert "changelog_model.json" in audit.prompts[0] and "#10" in audit.prompts[0] and "platform" in audit.prompts[0]
        assert out["summary"]["breaking_high"] == 1 and audit.diff_calls == ["10"]  # only the contract PR's diff is read

    def test_the_section_is_also_in_the_html_report(self, workspace, monkeypatch):
        audit = run_audit(workspace, monkeypatch, "https://github.com/o/r", gh=_gh_with(self.ROWS), codeowners=CODEOWNERS_TEXT, diff=PROTO_DIFF)
        assert "Changelog &amp; Blast Radius" in (audit.reports / "pr_audit_report.html").read_text()

    def test_without_a_model_file_the_changelog_is_built_from_facts(self, workspace, monkeypatch):
        audit = run_audit(workspace, monkeypatch, "https://github.com/o/r", gh=_gh_with(self.ROWS), codeowners=CODEOWNERS_TEXT, diff=PROTO_DIFF)
        out = json.loads((audit.reports / "changelog.json").read_text())
        assert out["mode"] == "facts" and {f["title"] for f in out["features"]} == {"Drop currency from Pay", "Tighten token expiry"}

    def test_no_changelog_flag_skips_everything(self, workspace, monkeypatch):
        audit = run_audit(workspace, monkeypatch, "https://github.com/o/r --no-changelog", gh=_gh_with(self.ROWS), codeowners=CODEOWNERS_TEXT)
        assert "Changelog & Blast Radius (specialist" not in audit.prompts[0]
        assert not (audit.reports / "changelog.json").exists() and "Changelog & Blast Radius" not in (audit.reports / "pr_audit_report.md").read_text()
        assert audit.diff_calls == []

    def test_breaking_diffs_zero_reads_no_diff_and_reports_path_only_candidates(self, workspace, monkeypatch):
        audit = run_audit(workspace, monkeypatch, "https://github.com/o/r --breaking-diffs 0", gh=_gh_with(self.ROWS), codeowners=CODEOWNERS_TEXT, diff=PROTO_DIFF)
        assert audit.diff_calls == []
        out = json.loads((audit.reports / "changelog.json").read_text())
        assert out["meta"]["diff_prs"] == 0 and out["summary"]["candidates_low"] >= 1

    def test_a_changelog_failure_does_not_fail_the_audit(self, workspace, monkeypatch):
        with patch("scripts.analyze.run_pr_audit.change_impact.build_facts", side_effect=RuntimeError("boom")):
            audit = run_audit(workspace, monkeypatch, "https://github.com/o/r", gh=_gh_with(self.ROWS))
        assert audit.code == 0 and (audit.reports / "pr_audit_report.md").exists() and not (audit.reports / "changelog.json").exists()

    def test_works_together_with_reviewers_and_dry_run(self, workspace, monkeypatch):
        audit = run_audit(workspace, monkeypatch, "https://github.com/o/r --reviewers alice --dry-run", gh=_gh_with(self.ROWS), codeowners=CODEOWNERS_TEXT, diff=PROTO_DIFF)
        assert audit.code == 0 and (audit.reports / "changelog.json").exists() and (audit.reports / "review_learnings.md").exists()


class TestChangelogBitbucket:
    def test_contract_change_owned_by_a_team_with_a_declared_breaking_title(self, bb_workspace, monkeypatch):
        bb = FakeBitbucket()
        inner = bb.__call__

        def with_contract_files(url, auth=None, timeout=None):
            resp = inner(url, auth=auth, timeout=timeout)
            if url.split("?")[0].endswith("/diffstat"):
                resp.json.return_value = {"values": [{"new": {"path": "proto/pay.proto"}, "lines_added": 0, "lines_removed": 1}], "next": ""}
            return resp

        bb.PRS[1]["title"] = "feat!: remove currency"
        audit = run_audit(bb_workspace, monkeypatch, "--n-prs 5", learnings=BB_LEARNINGS, bb=with_contract_files, codeowners=CODEOWNERS_TEXT, diff=PROTO_DIFF)
        assert audit.code == 0
        out = json.loads((audit.reports / "changelog.json").read_text())
        kinds = {b["kind"] for b in out["breaking_changes"]}
        assert {"field_removed", "declared_breaking"} <= kinds
        assert "platform" in {t for f in out["features"] for t in f["teams"]}


# -- Team scoping through the whole audit ----------------------------------------------------------------------------

class TestTeamScopeThroughTheAudit:
    """Org-config team/board defaults are in the environment (as the job YAML provides them); only --team may use them."""

    def _run(self, workspace, monkeypatch, message, **env):
        for k, v in {"JIRA_SPACE": "Org Default Team", "JIRA_BOARDS": "42", "PR_AUDIT_TEAM_MEMBERS": "someone", **env}.items():
            monkeypatch.setenv(k, v)
        seen = {}

        def keys(config):
            seen.update({k: config.get(k, "") for k in ("JIRA_SPACE", "JIRA_BOARDS", "PR_AUDIT_TEAM_MEMBERS")})
            return None  # no scope: the test is about what reaches the fetcher
        with patch("scripts.analyze.pr_fetcher._resolve_jira_issue_keys", side_effect=keys):
            audit = run_audit(workspace, monkeypatch, message)
        return audit, seen

    def test_without_team_the_org_defaults_never_reach_the_fetcher(self, workspace, monkeypatch):
        audit, seen = self._run(workspace, monkeypatch, "https://github.com/o/r --n-prs 5")
        assert audit.code == 0 and seen == {"JIRA_SPACE": "", "JIRA_BOARDS": "", "PR_AUDIT_TEAM_MEMBERS": ""}

    def test_a_named_team_reaches_the_fetcher_alone(self, workspace, monkeypatch):
        audit, seen = self._run(workspace, monkeypatch, "https://github.com/o/r --team Platform")
        assert audit.code == 0 and seen == {"JIRA_SPACE": "Platform", "JIRA_BOARDS": "", "PR_AUDIT_TEAM_MEMBERS": ""}

    def test_members_reach_the_fetcher_alone(self, workspace, monkeypatch):
        audit, seen = self._run(workspace, monkeypatch, "https://github.com/o/r --team alice,bob")
        assert seen == {"JIRA_SPACE": "", "JIRA_BOARDS": "", "PR_AUDIT_TEAM_MEMBERS": "alice,bob"}

    def test_a_bare_team_on_a_jira_run_uses_the_slack_users_team(self, bb_workspace, monkeypatch):
        monkeypatch.setenv("JIRA_BASE_URL", "https://j")
        monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "t")
        monkeypatch.setenv("SLACK_USER_TAG", "dev@example.com")
        with patch("scripts.analyze.run_pr_audit.resolve_user_team", return_value="Distributed") as mine:
            audit, seen = self._run(bb_workspace, monkeypatch, "--team")
        assert audit.code == 0 and seen["JIRA_SPACE"] == "Distributed" and seen["JIRA_BOARDS"] == ""
        assert mine.call_args[0][1] == "dev@example.com"
