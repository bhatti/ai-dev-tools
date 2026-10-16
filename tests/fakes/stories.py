"""One review story (humans, bots, threads/tasks, a merge commit) as GitHub and Bitbucket serve it.

`gh_node()` is the GraphQL PR node; `bb_story(bb)` registers the matching Bitbucket REST routes. Both must yield
`EXPECTED` from pr_signals: that equality is the GitHub <=> Bitbucket parity guarantee.
"""

from __future__ import annotations

from tests.fakes.bitbucket import FakeBitbucket


def user(login, typename="User"):
    return {"login": login, "__typename": typename}


def gh_node(number=7):
    """One GitHub PR as GraphQL returns it. Same story as bb_story() below."""
    c = lambda who, body, tn="User": {"body": body, "author": user(who, tn)}  # noqa: E731
    return {
        "number": number, "author": user("alice"),
        "comments": {"totalCount": 3, "nodes": [c("bob", "looks fine"), c("coderabbitai[bot]", "[critical] sql injection", "Bot"), c("alice", "fixed")]},
        "reviews": {"totalCount": 2, "nodes": [{"state": "APPROVED", "body": "", "author": user("bob")},
                                               {"state": "COMMENTED", "body": "summary", "author": user("copilot-reviewer", "Bot")}]},
        "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": "User", "login": "carol"}}]},
        "reviewThreads": {"totalCount": 2, "nodes": [
            {"isResolved": False, "comments": {"totalCount": 1, "nodes": [c("bob", "please rename")]}},
            {"isResolved": True, "comments": {"totalCount": 1, "nodes": [c("coderabbitai[bot]", "nit: spacing", "Bot")]}}]},
        "commits": {"totalCount": 3, "nodes": [{"commit": {"parents": {"totalCount": 1}}}, {"commit": {"parents": {"totalCount": 1}}},
                                               {"commit": {"parents": {"totalCount": 2}}}]},
    }


def gh_response(*nodes):
    return {"data": {"repository": {f"p{n['number']}": n for n in nodes}}}


EXPECTED = {
    "comments_human": 3, "comments_bot": 3, "major_comments_human": 0, "major_comments_bot": 1,
    "reviewers_human": 2, "reviewers_bot": 2, "approvers": ["bob"],
    "tasks_human": 1, "tasks_bot": 1, "tasks_unresolved": 1, "commits": 2,
}


def bb_story(bb: FakeBitbucket, number=5):
    """The same story as gh_node(), as Bitbucket serves it."""
    base = f"/repositories/ws/repo/pullrequests/{number}"
    u = lambda name, t="personal": {"display_name": name, "account_type": t}  # noqa: E731
    bb.add(base, {"author": u("alice"), "comment_count": 6, "task_count": 2, "reviewers": [u("carol")],
                  "participants": [{"role": "REVIEWER", "approved": True, "user": u("bob")}]})
    bb.add_pages(f"{base}/comments", [
        {"values": [{"user": u("bob"), "content": {"raw": "looks fine"}, "deleted": False},
                    {"user": u("coderabbitai[bot]", "app"), "content": {"raw": "[critical] sql injection"}, "deleted": False}]},
        {"values": [{"user": u("alice"), "content": {"raw": "fixed"}, "deleted": False},
                    {"user": u("bob"), "content": {"raw": "removed"}, "deleted": True},
                    {"user": u("bob"), "content": {"raw": "please rename"}, "inline": {"path": "a.py"}, "deleted": False},
                    {"user": u("copilot-reviewer", "app"), "content": {"raw": "summary"}, "deleted": False},
                    {"user": u("coderabbitai[bot]", "app"), "content": {"raw": "nit: spacing"}, "deleted": False}]}])
    bb.add_pages(f"{base}/tasks", [{"values": [{"state": "UNRESOLVED", "content": {"raw": "rename"}, "creator": u("bob")},
                                               {"state": "RESOLVED", "content": {"raw": "spacing"}, "creator": u("coderabbitai[bot]", "app")}]}])
    bb.add_pages(f"{base}/commits", [{"values": [{"hash": "a", "parents": [{"hash": "x"}]}, {"hash": "b", "parents": [{"hash": "a"}]},
                                                 {"hash": "m", "parents": [{"hash": "b"}, {"hash": "main"}]}]}])
