"""Tests for scripts/common/comment_select.py (budgeted, ranked review-comment selection)."""

from scripts.common.comment_select import select_comments


def _c(author, body, path="", ctype="comment"):
    return {"author": author, "body": body, "path": path, "type": ctype}


def _trivial(body):
    return body.strip().lower() in {"lgtm", "+1"}


class TestSelectComments:
    def test_priority_author_comments_never_dropped_or_cut_below_priority_cap(self):
        long_body = "x" * 3000
        comments = [_c("bob", "y" * 500) for _ in range(20)] + [_c("Senior", long_body)]
        sel, omitted = select_comments(comments, budget_chars=2000, priority_authors=("senior",), is_trivial=_trivial)
        senior = [c for c in sel if c["author"] == "Senior"]
        assert len(senior) == 1 and len(senior[0]["body"]) == 3000
        assert omitted == len(comments) - len(sel)

    def test_priority_body_capped_at_priority_cap(self):
        sel, _ = select_comments([_c("s", "z" * 9000)], budget_chars=100, priority_authors=("s",),
                                 is_trivial=_trivial, priority_cap=4000)
        assert len(sel[0]["body"]) <= 4000 + 3  # ellipsis marker allowed

    def test_alias_match_for_priority(self):
        c = _c("Display Name", "w" * 300)
        c["author_alias"] = "nick"
        sel, _ = select_comments([c, _c("other", "q" * 300)], budget_chars=10, priority_authors=("NICK",), is_trivial=_trivial)
        assert any(x["author"] == "Display Name" for x in sel)

    def test_budget_respected_for_non_priority(self):
        comments = [_c(f"u{i}", "a" * 400) for i in range(30)]
        sel, omitted = select_comments(comments, budget_chars=2000, is_trivial=_trivial)
        assert sum(len(c["body"]) for c in sel) <= 2000
        assert omitted == 30 - len(sel) > 0

    def test_one_per_commenter_before_a_second_from_the_same_person(self):
        comments = [_c("a", "1" * 500), _c("a", "2" * 500), _c("b", "3" * 100)]
        sel, _ = select_comments(comments, budget_chars=700, is_trivial=_trivial)
        assert {c["author"] for c in sel} == {"a", "b"}

    def test_inline_and_longer_outrank_short_general(self):
        comments = [_c("a", "short"), _c("b", "m" * 200, path="f.py", ctype="review_comment"), _c("c", "n" * 200)]
        sel, _ = select_comments(comments, budget_chars=250, is_trivial=_trivial)
        # inline "b" is ranked first and consumes the budget; "c" (200) no longer fits, "a" (5) does
        assert {c["author"] for c in sel} == {"a", "b"}

    def test_trivial_comments_only_fill_leftover_budget(self):
        comments = [_c("a", "LGTM"), _c("b", "k" * 300)]
        sel, _ = select_comments(comments, budget_chars=300, is_trivial=_trivial)
        assert [c["author"] for c in sel] == ["b"]

    def test_original_order_preserved_and_input_not_mutated(self):
        comments = [_c("a", "1" * 50), _c("b", "2" * 50), _c("c", "3" * 50)]
        snapshot = [dict(c) for c in comments]
        sel, _ = select_comments(comments, budget_chars=10_000, is_trivial=_trivial)
        assert [c["author"] for c in sel] == ["a", "b", "c"]
        assert comments == snapshot

    def test_per_comment_cap_truncates_with_marker(self):
        sel, _ = select_comments([_c("a", "t" * 5000)], budget_chars=10_000, is_trivial=_trivial, per_comment_cap=1500)
        assert len(sel[0]["body"]) == 1500 + 1 and sel[0]["body"].endswith("…")

    def test_empty(self):
        assert select_comments([], budget_chars=100, is_trivial=_trivial) == ([], 0)
