"""Tests for scripts/analyze/reviewer_scope.py (pure: names -> people -> queries -> matching PRs)."""

from scripts.analyze import reviewer_scope as rs

DIRECTORY = [
    {"display_name": "Casey Jordan", "nickname": "Casey Jordan", "uuid": "{00000000-0000-4000-8000-0000000000b2}", "account_id": "712020:00000000-0000-4000-8000-000000000001"},
    {"display_name": "Joaquín Bravo", "nickname": "Joaquin Bravo", "uuid": "{00000000-0000-4000-8000-0000000000c3}", "account_id": "0123456789abcdef01234567"},
    {"display_name": "Pat Duncan", "nickname": "Pat Duncan", "uuid": "{aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}", "account_id": "0123456789abcdef012345aa"},
    {"display_name": "Pat Holloway", "nickname": "pholloway"},
]


def _pr(num, commenters=(), groups=(), author="dev"):
    human = [{"author": a, "author_alias": al, "author_id": i, "body": "x" * 30} for a, al, i in commenters]
    return {"number": num, "author": author, "human_comments": human, "approvers": [], "reviewers": [], "participant_groups": list(groups)}


class TestResolve:
    def test_initials_first_name_and_accents_resolve_through_the_directory(self):
        scope = rs.resolve("CJ,pat,JB,bravo", DIRECTORY)
        assert [p["display_name"] for p in scope["CJ"]] == ["Casey Jordan"]
        assert [p["display_name"] for p in scope["pat"]] == ["Pat Duncan", "Pat Holloway"]
        assert [p["display_name"] for p in scope["JB"]] == ["Joaquín Bravo"]
        assert [p["display_name"] for p in scope["bravo"]] == ["Joaquín Bravo"]

    def test_unknown_name_stays_literal_so_an_exact_id_still_works(self):
        scope = rs.resolve("zzz", DIRECTORY)
        assert scope["zzz"] == [{"display_name": "zzz", "nickname": "zzz"}]
        assert rs.resolve("zzz", [])["zzz"][0]["nickname"] == "zzz"

    def test_list_parsing(self):
        assert rs.reviewer_list(" a, B ,a,,<no value>") == ["a", "B"]
        assert rs.literal_scope("") == {}


class TestQueries:
    def test_bbql_prefers_account_id_then_uuid_then_nickname_and_escapes(self):
        scope = rs.resolve("CJ,pat", DIRECTORY)
        q = rs.bbql(scope).split(" OR ")
        assert q[0] == 'reviewers.account_id="712020:00000000-0000-4000-8000-000000000001"'
        assert q[1] == 'reviewers.account_id="0123456789abcdef012345aa"'
        assert q[2] == 'reviewers.nickname="pholloway"'
        lit = rs.bbql(rs.literal_scope('ev"il\\x,{00000000-0000-4000-8000-0000000000b2},0123456789abcdef01234567'))
        assert lit.split(" OR ")[0] == 'reviewers.nickname="ev\\"il\\\\x"'
        assert 'reviewers.uuid="{00000000-0000-4000-8000-0000000000b2}"' in lit and 'reviewers.account_id="0123456789abcdef01234567"' in lit

    def test_bbql_has_no_duplicate_clauses(self):
        scope = {"a": [DIRECTORY[0]], "b": [DIRECTORY[0]]}
        assert rs.bbql(scope).count("OR") == 0

    def test_github_searches_exclude_author_and_skip_non_logins(self):
        searches, skipped = rs.gh_searches(rs.literal_scope("alice, Bob Smith ,alice,x y:z"))
        assert searches == ["reviewed-by:alice -author:alice", "commenter:alice -author:alice"]
        assert skipped == ["Bob Smith", "x y:z"]


class TestMatching:
    def test_prefilter_uses_every_identity_and_keeps_rows_without_participants(self):
        scope = rs.resolve("CJ", DIRECTORY)
        rows = [
            {"id": 1, "participants": [{"user": {"display_name": "Casey Jordan", "nickname": "Casey Jordan"}, "role": "PARTICIPANT"}]},
            {"id": 2, "participants": [{"user": {"display_name": "Zed", "nickname": "zed"}, "role": "REVIEWER"}]},
            {"id": 3},
            {"id": 4, "participants": [{"user": {"display_name": "Casey Jordan"}, "role": "AUTHOR"}]},
            {"id": 5, "participants": [{"user": {"account_id": "712020:00000000-0000-4000-8000-000000000001"}, "role": "REVIEWER"}]},
        ]
        assert [r["id"] for r in rs.prefilter_bb_rows(rows, scope)] == [1, 3, 5]
        assert rs.prefilter_bb_rows(rows, {}) == rows

    def test_filter_matches_requested_names_through_any_identity_and_ignores_the_authors_own_comments(self):
        scope = rs.resolve("CJ,pat", DIRECTORY)
        prs = [
            _pr(1, commenters=[("Casey Jordan", "Casey Jordan", "712020:00000000-0000-4000-8000-000000000001")]),
            _pr(2, groups=[["Pat Duncan", "Pat Duncan", "{aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}"]]),
            _pr(3, commenters=[("Zed", "zed", "")]),
            _pr(4, author="Casey Jordan", commenters=[("Casey Jordan", "Casey Jordan", "")]),
        ]
        kept = rs.filter_prs(prs, scope)
        assert [(p["number"], p["matched_reviewers"]) for p in kept] == [(1, ["CJ"]), (2, ["pat"])]
        assert rs.filter_prs(prs, {}) == prs

    def test_identities_lists_only_the_people_actually_seen(self):
        scope = rs.resolve("pat,CJ,JB", DIRECTORY)
        prs = [_pr(2, groups=[["Pat Duncan", "Pat Duncan", "{aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa}"]])]
        found = rs.identities(prs, scope)
        assert found == {"pat": ["Pat Duncan"]}  # only the person actually seen, names only (no ids)

    def test_identities_empty_without_prs(self):
        assert rs.identities([], rs.resolve("CJ", DIRECTORY)) == {}
