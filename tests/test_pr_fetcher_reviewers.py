"""Tests for the --reviewers filters and ranked comment context in pr_fetcher.py."""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from scripts.analyze.pr_fetcher import (
    _bb_reviewer_query,
    _filter_by_reviewers,
    _gh_reviewer_searches,
    _prefilter_bb_raw,
    build_pr_context,
    fetch_bitbucket_prs,
    fetch_github_prs,
    unmatched_reviewers,
)


def _pr(num, author="dev", commenters=(), approvers=(), reviewers=(), comments=None):
    human = comments if comments is not None else [{"author": a, "body": "please fix " + a, "type": "comment"} for a in commenters]
    return {"number": num, "title": f"t{num}", "author": author, "state": "merged", "human_comments": human,
            "approvers": list(approvers), "reviewers": list(reviewers), "all_comments": human,
            "files_changed": 1, "additions": 1, "deletions": 1}


class TestFilterByReviewers:
    def test_matches_commenter_approver_or_assigned_reviewer_case_insensitively(self):
        prs = [_pr(1, commenters=["Alice"]), _pr(2, approvers=["ALICE"]), _pr(3, reviewers=["alice"]), _pr(4, commenters=["zed"])]
        assert [p["number"] for p in _filter_by_reviewers(prs, "alice")] == [1, 2, 3]

    def test_pr_authored_by_reviewer_does_not_count_without_review_activity(self):
        prs = [_pr(1, author="alice", commenters=["alice"]), _pr(2, author="bob", commenters=["alice"])]
        assert [p["number"] for p in _filter_by_reviewers(prs, "alice")] == [2]

    def test_matches_bitbucket_alias_nickname(self):
        c = [{"author": "Alice Smith", "author_alias": "asmith", "body": "x" * 50, "type": "comment"}]
        assert len(_filter_by_reviewers([_pr(1, comments=c)], "ASmith")) == 1

    def test_records_matched_reviewers_and_empty_list_means_no_filter(self):
        prs = [_pr(1, commenters=["Alice", "zed"])]
        out = _filter_by_reviewers(prs, "alice,bob")
        assert out[0]["matched_reviewers"] == ["alice"]
        assert _filter_by_reviewers(prs, "") == prs

    def test_unmatched_names_reported(self):
        kept = _filter_by_reviewers([_pr(1, commenters=["alice"])], "alice,Typo")
        assert unmatched_reviewers(kept, "alice,Typo") == ["Typo"]


class TestPrefilterBbRaw:
    def test_uses_participants_display_name_nickname_uuid_account_id_and_keeps_unknown(self):
        raw = [
            {"id": 1, "participants": [{"user": {"display_name": "Alice Smith", "nickname": "asmith"}, "role": "REVIEWER"}]},
            {"id": 2, "participants": [{"user": {"display_name": "Zed", "nickname": "zed"}, "role": "REVIEWER"}]},
            {"id": 3},  # no participant data: cannot prove irrelevant, keep
            {"id": 4, "participants": [{"user": {"nickname": "asmith"}, "role": "AUTHOR"}]},
            {"id": 5, "participants": [{"user": {"nickname": "x", "account_id": "712020:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}, "role": "PARTICIPANT"}]},
        ]
        assert [r["id"] for r in _prefilter_bb_raw(raw, "asmith")] == [1, 3]
        assert [r["id"] for r in _prefilter_bb_raw(raw, "Alice Smith")] == [1, 3]
        assert [r["id"] for r in _prefilter_bb_raw(raw, "712020:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")] == [3, 5]


class TestQueryBuilders:
    def test_github_searches_exclude_author_and_skip_non_logins(self):
        searches, skipped = _gh_reviewer_searches("alice, Bob Smith ,alice,x y:z")
        assert searches == ["reviewed-by:alice -author:alice", "commenter:alice -author:alice"]
        assert skipped == ["Bob Smith", "x y:z"]

    def test_bitbucket_query_picks_field_by_identifier_shape_and_escapes(self):
        q = _bb_reviewer_query('asmith,{b8ceb65c-4e2e-43d3-8a54-fe5f9cd2b3b2},5c355119b393bf4ce9561ec3,712020:55b7c8c6-7cd1-40a5-affb-5ecda0b7c406,ev"il\\x')
        parts = q.split(" OR ")
        assert parts[0] == 'reviewers.nickname="asmith"'
        assert parts[1] == 'reviewers.uuid="{b8ceb65c-4e2e-43d3-8a54-fe5f9cd2b3b2}"'
        assert parts[2] == 'reviewers.account_id="5c355119b393bf4ce9561ec3"'
        assert parts[3].startswith('reviewers.account_id="712020:')
        assert parts[4] == 'reviewers.nickname="ev\\"il\\\\x"'  # quote and backslash escaped


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

    def __init__(self, by_q):
        self.by_q, self.urls = by_q, []

    def __call__(self, url, auth=None, timeout=None):
        self.urls.append(url)
        parsed = urlparse(url)
        resp = MagicMock(status_code=200)
        if parsed.path.endswith("/pullrequests"):
            q = parse_qs(parsed.query).get("q", [""])[0]
            resp.json.return_value = {"values": self.by_q.get(q, []), "next": ""}
        else:  # comments / diffstat
            resp.json.return_value = {"values": [], "next": ""}
        return resp

    def list_queries(self):
        return [parse_qs(urlparse(u).query).get("q", [""])[0] for u in self.urls if urlparse(u).path.endswith("/pullrequests")]

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
