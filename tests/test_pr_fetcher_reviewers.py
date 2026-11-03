"""Tests for the --reviewers filters and ranked comment context in pr_fetcher.py."""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from scripts.analyze.pr_fetcher import (
    build_pr_context,
    fetch_bitbucket_prs,
    fetch_github_prs,
)


def _pr(num, author="dev", commenters=(), approvers=(), reviewers=(), comments=None):
    human = comments if comments is not None else [{"author": a, "body": "please fix " + a, "type": "comment"} for a in commenters]
    return {"number": num, "title": f"t{num}", "author": author, "state": "merged", "human_comments": human,
            "approvers": list(approvers), "reviewers": list(reviewers), "all_comments": human,
            "files_changed": 1, "additions": 1, "deletions": 1}


# -- fetch_github_prs: server-side search per reviewer ---------------------------

def _gh_row(num, author="dev", reviewer="alice", merged=True):
    return {"number": num, "title": f"t{num}", "state": "MERGED" if merged else "CLOSED", "body": "", "author": {"login": author},
            "createdAt": "2026-01-01T00:00:00Z", "mergedAt": "2026-01-02T00:00:00Z" if merged else None, "url": f"https://x/pull/{num}",
            "headRefName": "b", "baseRefName": "main", "labels": [], "files": [{"path": "a.py", "additions": 3, "deletions": 1}],
            "comments": [], "reviewDecision": "",
            "reviews": [{"author": {"login": reviewer}, "state": "COMMENTED", "body": f"please change {num} because of locking"}]}


class FakeGh:
    """Stands in for `gh pr list`; records commands and answers by (--state, --search)."""

    def __init__(self, answers):
        self.answers, self.cmds = answers, []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        state = cmd[cmd.index("--state") + 1]
        search = cmd[cmd.index("--search") + 1] if "--search" in cmd else ""
        return MagicMock(returncode=0, stdout=json.dumps(self.answers.get((state, search), [])), stderr="")


@pytest.fixture
def gh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    return {"GH_ORG": "o", "GH_REPO": "r"}


class TestFetchGithubReviewers:
    def _fetch(self, config, fake, n=10, states=None):
        with patch("scripts.analyze.pr_fetcher.subprocess.run", side_effect=fake), \
             patch("scripts.analyze.pr_fetcher._fetch_gh_review_comments", return_value=[]):
            return fetch_github_prs(config, n_prs=n, states=states)

    def test_one_search_per_qualifier_and_state_union_deduped_newest_first_and_capped(self, gh_env):
        fake = FakeGh({
            ("merged", "reviewed-by:alice -author:alice"): [_gh_row(5), _gh_row(3)],
            ("merged", "commenter:alice -author:alice"): [_gh_row(5), _gh_row(4)],
            ("closed", "reviewed-by:alice -author:alice"): [_gh_row(9, merged=False), _gh_row(2)],  # 2 is merged-looking: filtered
        })
        fake.answers[("closed", "reviewed-by:alice -author:alice")][1]["mergedAt"] = "2026-01-03T00:00:00Z"
        prs = self._fetch({**gh_env, "PR_AUDIT_REVIEWERS": "alice"}, fake, n=3)
        assert [p["number"] for p in prs] == [9, 5, 4]  # 3 dropped by the cap; PR 2 is merged so not "declined"
        searches = [c[c.index("--search") + 1] for c in fake.cmds]
        assert "reviewed-by:alice -author:alice" in searches and "commenter:alice -author:alice" in searches
        assert all(c[c.index("--limit") + 1] == "3" for c in fake.cmds)  # server-side limit, no 10x scan

    def test_matched_reviewers_recorded_and_author_excluded(self, gh_env):
        fake = FakeGh({("merged", "reviewed-by:alice -author:alice"): [_gh_row(1, author="dev", reviewer="alice")]})
        prs = self._fetch({**gh_env, "PR_AUDIT_REVIEWERS": "alice"}, fake)
        assert prs[0]["matched_reviewers"] == ["alice"]

    def test_non_login_reviewer_runs_no_search(self, gh_env, capsys):
        fake = FakeGh({})
        assert self._fetch({**gh_env, "PR_AUDIT_REVIEWERS": "Alice Smith"}, fake) == []
        assert fake.cmds == []  # nothing is interpolated into a search query
        assert "must be logins" in capsys.readouterr().err

    def test_without_reviewers_no_search_flag(self, gh_env):
        fake = FakeGh({("merged", ""): [_gh_row(1)]})
        prs = self._fetch(gh_env, fake)
        assert [p["number"] for p in prs] == [1]
        assert all("--search" not in c for c in fake.cmds)

    def test_unrendered_template_placeholder_means_no_filter(self, gh_env):
        fake = FakeGh({("merged", ""): [_gh_row(1)]})
        prs = self._fetch({**gh_env, "PR_AUDIT_REVIEWERS": "<no value>"}, fake)
        assert [p["number"] for p in prs] == [1] and all("--search" not in c for c in fake.cmds)

    def test_open_state_uses_open_search(self, gh_env):
        fake = FakeGh({("open", "commenter:alice -author:alice"): [_gh_row(7, merged=False)]})
        prs = self._fetch({**gh_env, "PR_AUDIT_REVIEWERS": "alice"}, fake, states=["open"])
        assert [p["number"] for p in prs] == [7]


# -- fetch_bitbucket_prs: BBQL on reviewers, then bounded scan for commenters ------

def _bb_row(pr_id, author="dev", participants=()):
    return {"id": pr_id, "title": f"t{pr_id}", "state": "MERGED", "updated_on": "2026-01-02T00:00:00Z", "created_on": "2026-01-01T00:00:00Z",
            "author": {"display_name": author, "nickname": author}, "description": "",
            "source": {"branch": {"name": "b"}}, "destination": {"branch": {"name": "main"}},
            "links": {"html": {"href": f"https://bb/pr/{pr_id}"}},
            "participants": [{"user": {"display_name": d, "nickname": n, "uuid": u, "account_id": a}, "role": r, "approved": ap}
                             for d, n, u, a, r, ap in participants]}


class FakeBb:
    """Stands in for requests.get against the Bitbucket API; answers list requests by their decoded q parameter."""

    def __init__(self, by_q, members=None, members_status=200):
        self.by_q, self.urls, self.members, self.members_status = by_q, [], members or [], members_status

    def __call__(self, url, auth=None, timeout=None):
        import requests
        self.urls.append(url)
        parsed = urlparse(url)
        resp = MagicMock(status_code=200)
        if parsed.path.endswith("/members"):
            resp.status_code = self.members_status
            if self.members_status >= 400:
                resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
            resp.json.return_value = {"values": [{"user": u} for u in self.members], "next": ""}
        elif parsed.path.endswith("/pullrequests"):
            q = parse_qs(parsed.query).get("q", [""])[0]
            resp.json.return_value = {"values": self.by_q.get(q, []), "next": ""}
        else:  # comments / diffstat
            resp.json.return_value = {"values": [], "next": ""}
        return resp

    def list_queries(self):
        return [parse_qs(urlparse(u).query).get("q", [""])[0] for u in self.urls if urlparse(u).path.endswith("/pullrequests")]

    def member_calls(self):
        return [u for u in self.urls if urlparse(u).path.endswith("/members")]

    def detail_pr_ids(self):
        return sorted({int(u.split("/pullrequests/")[1].split("/")[0]) for u in self.urls if "/pullrequests/" in u})


@pytest.fixture
def bb_config(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("BITBUCKET_API_BASE", raising=False)
    return {"BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "repo"}


class TestBbDiffstat:
    def test_binary_file_entries_with_null_line_counts_become_zero(self):
        from scripts.analyze.pr_fetcher import _fetch_bb_diffstat
        resp = MagicMock()
        resp.json.return_value = {"values": [{"new": {"path": "a.png"}, "lines_added": None, "lines_removed": None}]}
        with patch("requests.get", return_value=resp):
            files = _fetch_bb_diffstat("https://bb", 1, None)
        assert files == [{"path": "a.png", "lines_added": 0, "lines_removed": 0}]


class TestBbListDedup:
    def test_a_pr_returned_once_per_matching_reviewer_is_kept_once(self):
        from scripts.analyze.pr_fetcher import _bb_list_prs
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"values": [{"id": 5}, {"id": 5}, {"id": 5}, {"id": 6}], "next": ""}
        with patch("requests.get", return_value=resp):
            assert [r["id"] for r in _bb_list_prs("https://bb/x", None, 10)] == [5, 6]


class TestBbRetry:
    def _resp(self, status, headers=None):
        import requests
        r = MagicMock(status_code=status, headers=headers or {})
        r.json.return_value = {"values": [{"id": 1}], "next": ""}
        if status >= 400:
            r.raise_for_status.side_effect = requests.HTTPError(response=r)
        return r

    def test_rate_limit_is_retried_honoring_retry_after_then_succeeds(self):
        from scripts.analyze.pr_fetcher import _bb_list_prs
        with patch("requests.get", side_effect=[self._resp(429, {"Retry-After": "2"}), self._resp(200)]) as get, \
             patch("scripts.common.http_retry.time.sleep") as sleep:
            rows = _bb_list_prs("https://bb/x", None, 10)
        assert rows == [{"id": 1}] and get.call_count == 2
        sleep.assert_called_once_with(2.0)

    def test_persistent_failure_returns_what_was_read_and_reports(self, capsys):
        from scripts.analyze.pr_fetcher import _bb_list_prs
        with patch("requests.get", return_value=self._resp(503)), patch("scripts.common.http_retry.time.sleep"):
            assert _bb_list_prs("https://bb/x", None, 10) == []
        assert "Bitbucket API error" in capsys.readouterr().err

    def test_client_error_is_not_retried(self):
        from scripts.analyze.pr_fetcher import _bb_list_prs
        with patch("requests.get", return_value=self._resp(404)) as get:
            assert _bb_list_prs("https://bb/x", None, 10) == []
        assert get.call_count == 1


class TestFetchBitbucketReviewers:
    ALICE = ("Alice Smith", "asmith", "{u-1}", "acc-1", "REVIEWER", True)

    def test_server_side_query_then_details_only_for_matches(self, bb_config):
        fake = FakeBb({'reviewers.nickname="asmith"': [_bb_row(1, participants=[self.ALICE]), _bb_row(2, participants=[self.ALICE])]})
        with patch("requests.get", side_effect=fake):
            prs = fetch_bitbucket_prs({**bb_config, "PR_AUDIT_REVIEWERS": "asmith"}, n_prs=2)
        assert sorted(p["number"] for p in prs) == [1, 2]
        assert fake.list_queries() == ['reviewers.nickname="asmith"']  # enough matches: no scan
        assert fake.detail_pr_ids() == [1, 2]

    def test_nickname_differs_from_display_name_still_matches_after_post_filter(self, bb_config):
        fake = FakeBb({'reviewers.nickname="asmith"': [_bb_row(1, participants=[self.ALICE])]})
        with patch("requests.get", side_effect=fake):
            prs = fetch_bitbucket_prs({**bb_config, "PR_AUDIT_REVIEWERS": "asmith"}, n_prs=1)
        assert len(prs) == 1 and prs[0]["matched_reviewers"] == ["asmith"]

    def test_scans_commented_prs_when_assigned_reviewers_are_not_enough(self, bb_config):
        commenter = ("Alice Smith", "asmith", "{u-1}", "acc-1", "PARTICIPANT", False)
        other = ("Zed", "zed", "{u-2}", "acc-2", "PARTICIPANT", False)
        fake = FakeBb({
            'reviewers.nickname="asmith"': [_bb_row(1, participants=[self.ALICE])],
            "comment_count>0": [_bb_row(1, participants=[self.ALICE]), _bb_row(2, participants=[commenter]), _bb_row(3, participants=[other])],
        })
        with patch("requests.get", side_effect=fake):
            fetch_bitbucket_prs({**bb_config, "PR_AUDIT_REVIEWERS": "asmith"}, n_prs=5)
        assert fake.list_queries() == ['reviewers.nickname="asmith"', "comment_count>0"]
        assert fake.detail_pr_ids() == [1, 2]  # PR 3 (someone else) never enriched; PR 1 not duplicated

    def test_display_name_input_matches_through_scan(self, bb_config):
        commenter = ("Alice Smith", "asmith", "{u-1}", "acc-1", "PARTICIPANT", False)
        fake = FakeBb({"comment_count>0": [_bb_row(2, participants=[commenter])]})
        with patch("requests.get", side_effect=fake):
            fetch_bitbucket_prs({**bb_config, "PR_AUDIT_REVIEWERS": "Alice Smith"}, n_prs=5)
        assert fake.detail_pr_ids() == [2]

    def test_unrendered_template_placeholder_means_no_filter(self, bb_config):
        fake = FakeBb({"": [_bb_row(1)]})
        with patch("requests.get", side_effect=fake):
            prs = fetch_bitbucket_prs({**bb_config, "PR_AUDIT_REVIEWERS": "<no value>"}, n_prs=5)
        assert fake.list_queries() == [""] and [p["number"] for p in prs] == [1]

    def test_without_reviewers_no_q_parameter(self, bb_config):
        fake = FakeBb({"": [_bb_row(1)]})
        with patch("requests.get", side_effect=fake):
            prs = fetch_bitbucket_prs(bb_config, n_prs=5)
        assert fake.list_queries() == [""] and [p["number"] for p in prs] == [1]


class TestBuildContextCommentDepth:
    def test_priority_reviewer_comment_included_in_full_beyond_old_300_char_cap(self):
        long = "WHY: this lock is held across I/O. " * 40  # ~1400 chars
        pr = _pr(1, comments=[{"author": f"u{i}", "body": "filler " * 30, "type": "comment"} for i in range(8)]
                 + [{"author": "Senior", "body": long, "type": "review_comment", "path": "a.py"}])
        text = build_pr_context([pr], max_chars=4000, priority_authors=("senior",))
        assert long.strip() in text

    def test_default_keeps_more_than_five_comments_when_budget_allows(self):
        pr = _pr(1, comments=[{"author": f"u{i}", "body": f"comment-number-{i} " + "z" * 100, "type": "comment"} for i in range(9)])
        text = build_pr_context([pr], max_chars=100_000)
        assert all(f"comment-number-{i}" in text for i in range(9))


# -- the Jira dev-status supplement must not re-add PRs the reviewers filter removed ----------

class TestReviewerFilterIsFinal:
    def test_dev_status_supplement_cannot_bypass_the_reviewers_filter(self, bb_config):
        """Regression: with a Jira filter active, `_supplement_from_dev_status` appends PRs found through Jira links,
        fetched without any reviewer filter. The reviewers filter must run after it."""
        stranger = _pr(99, author="dev", commenters=["someone-else"])
        stranger["linked_issue"] = None
        fake = FakeBb({})  # server-side query and scan find nothing
        cfg = {**bb_config, "PR_AUDIT_REVIEWERS": "asmith", "JIRA_BOARDS": "1"}
        with patch("requests.get", side_effect=fake), \
             patch("scripts.analyze.pr_fetcher._resolve_jira_issue_keys", return_value=({"K-1"}, [{"key": "K-1", "id": "1"}])), \
             patch("scripts.analyze.pr_fetcher._supplement_from_dev_status", side_effect=lambda c, issues, prs: prs + [stranger]):
            prs = fetch_bitbucket_prs(cfg, n_prs=5)
        assert prs == []

    def test_supplemented_pr_that_does_match_is_kept(self, bb_config):
        match = _pr(98, author="dev", commenters=["asmith"])
        fake = FakeBb({})
        cfg = {**bb_config, "PR_AUDIT_REVIEWERS": "asmith", "JIRA_BOARDS": "1"}
        with patch("requests.get", side_effect=fake), \
             patch("scripts.analyze.pr_fetcher._resolve_jira_issue_keys", return_value=({"K-1"}, [{"key": "K-1", "id": "1"}])), \
             patch("scripts.analyze.pr_fetcher._supplement_from_dev_status", side_effect=lambda c, issues, prs: prs + [match]):
            prs = fetch_bitbucket_prs(cfg, n_prs=5)
        assert [p["number"] for p in prs] == [98]

    def test_github_dev_status_supplement_is_also_filtered(self, gh_env):
        stranger = _pr(77, author="dev", commenters=["someone-else"])
        fake = FakeGh({})
        cfg = {**gh_env, "PR_AUDIT_REVIEWERS": "alice", "JIRA_BOARDS": "1", "JIRA_BASE_URL": "x"}
        with patch("scripts.analyze.pr_fetcher.subprocess.run", side_effect=fake), \
             patch("scripts.analyze.pr_fetcher._fetch_gh_review_comments", return_value=[]), \
             patch("scripts.analyze.pr_fetcher._resolve_jira_issue_keys", return_value=({"K-1"}, [{"key": "K-1", "id": "1"}])), \
             patch("scripts.analyze.pr_fetcher._supplement_from_dev_status", side_effect=lambda c, issues, prs: prs + [stranger]):
            assert fetch_github_prs(cfg, n_prs=5) == []




# -- names resolved through the workspace directory (the CJ / pat / JB case) --------------------------------

CHRIS = {"display_name": "Casey Jordan", "nickname": "Casey Jordan", "uuid": "{00000000-0000-4000-8000-0000000000b2}",
         "account_id": "712020:00000000-0000-4000-8000-000000000001"}
CHRIS_P = ("Casey Jordan", "Casey Jordan", CHRIS["uuid"], CHRIS["account_id"])


class TestBitbucketDirectoryResolution:
    def test_initials_resolve_to_the_person_and_query_uses_the_account_id(self, bb_config):
        fake = FakeBb({f'reviewers.account_id="{CHRIS["account_id"]}"': [_bb_row(1, participants=[CHRIS_P + ("REVIEWER", True)])]},
                      members=[CHRIS, {"display_name": "Zed", "nickname": "zed"}])
        cfg = {**bb_config, "PR_AUDIT_REVIEWERS": "CJ"}
        with patch("requests.get", side_effect=fake):
            prs = fetch_bitbucket_prs(cfg, n_prs=1)
        assert fake.list_queries() == [f'reviewers.account_id="{CHRIS["account_id"]}"']
        assert [(p["number"], p["matched_reviewers"]) for p in prs] == [(1, ["CJ"])]
        assert [p["display_name"] for p in cfg["_reviewer_scope"]["CJ"]] == ["Casey Jordan"]
        assert len(fake.member_calls()) == 1  # the directory is read once per run

    def test_first_name_covers_every_person_with_it_and_commenters_found_by_scan(self, bb_config):
        pat_d = ("Pat Duncan", "Pat Duncan", "{u-d}", "acc-d", "PARTICIPANT", False)
        fake = FakeBb({"comment_count>0": [_bb_row(7, participants=[pat_d])]},
                      members=[{"display_name": "Pat Duncan", "nickname": "Pat Duncan", "uuid": "{u-d}", "account_id": "acc-d"},
                               {"display_name": "Pat Holloway", "nickname": "pholloway"}])
        cfg = {**bb_config, "PR_AUDIT_REVIEWERS": "pat"}
        with patch("requests.get", side_effect=fake):
            prs = fetch_bitbucket_prs(cfg, n_prs=5)
        assert [(p["number"], p["matched_reviewers"]) for p in prs] == [(7, ["pat"])]

    def test_members_api_refused_falls_back_to_participants_of_recent_prs(self, bb_config):
        fake = FakeBb({"": [_bb_row(3, participants=[CHRIS_P + ("REVIEWER", True)])],
                       f'reviewers.account_id="{CHRIS["account_id"]}"': [_bb_row(3, participants=[CHRIS_P + ("REVIEWER", True)])]},
                      members_status=403)
        cfg = {**bb_config, "PR_AUDIT_REVIEWERS": "CJ"}
        with patch("requests.get", side_effect=fake):
            prs = fetch_bitbucket_prs(cfg, n_prs=1)
        assert [p["number"] for p in prs] == [3]
        assert fake.list_queries()[0] == ""  # directory scan first, then the id query

    def test_unresolvable_name_returns_no_prs_and_no_error(self, bb_config):
        fake = FakeBb({}, members=[CHRIS])
        with patch("requests.get", side_effect=fake):
            assert fetch_bitbucket_prs({**bb_config, "PR_AUDIT_REVIEWERS": "nobody"}, n_prs=3) == []


class TestEveryRequestedNameIsRepresented:
    """A small --n-prs must not let the most active reviewer crowd the others out (they would be reported "not found")."""

    def test_bitbucket_interleaves_one_query_per_name(self, bb_config):
        fake = FakeBb({'reviewers.nickname="a"': [_bb_row(10, participants=[("A", "a", "{u-a}", "acc-a", "REVIEWER", True)]),
                                                  _bb_row(9, participants=[("A", "a", "{u-a}", "acc-a", "REVIEWER", True)]),
                                                  _bb_row(8, participants=[("A", "a", "{u-a}", "acc-a", "REVIEWER", True)])],
                       'reviewers.nickname="b"': [_bb_row(2, participants=[("B", "b", "{u-b}", "acc-b", "REVIEWER", True)])]})
        with patch("requests.get", side_effect=fake):
            prs = fetch_bitbucket_prs({**bb_config, "PR_AUDIT_REVIEWERS": "a,b"}, n_prs=2)
        assert fake.list_queries() == ['reviewers.nickname="a"', 'reviewers.nickname="b"']
        assert [p["number"] for p in prs] == [10, 2]  # b's only PR is kept although a has newer ones

    def test_github_interleaves_per_login(self, gh_env):
        fake = FakeGh({("merged", "reviewed-by:a -author:a"): [_gh_row(30, reviewer="a"), _gh_row(29, reviewer="a"), _gh_row(28, reviewer="a")],
                       ("merged", "reviewed-by:b -author:b"): [_gh_row(3, reviewer="b")]})
        with patch("scripts.analyze.pr_fetcher.subprocess.run", side_effect=fake), \
             patch("scripts.analyze.pr_fetcher._fetch_gh_review_comments", return_value=[]):
            prs = fetch_github_prs({**gh_env, "PR_AUDIT_REVIEWERS": "a,b"}, n_prs=2)
        assert [p["number"] for p in prs] == [30, 3]

    def test_interleave_helper_dedupes_and_keeps_order(self):
        from scripts.analyze.pr_fetcher import _interleave
        out = _interleave([[{"id": 1}, {"id": 2}, {"id": 3}], [{"id": 1}, {"id": 9}]], "id")
        assert [r["id"] for r in out] == [1, 2, 9, 3]
        assert _interleave([], "id") == [] and _interleave([[]], "id") == []
