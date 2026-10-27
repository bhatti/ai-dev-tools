"""Tests for scripts/tickets/load.py: sprint totals by lane, carry-over, and the flags a planner needs."""

from __future__ import annotations

from scripts.tickets import load
from scripts.tickets.config import ReadinessConfig
from scripts.tickets.model import new_ticket


def T(id, lane="pending", points=3.0, sprints=None, subtask=False, status=None):
    status = status or {"pending": "To Do", "in_progress": "In Progress", "review": "In Review", "done": "Done"}[lane]
    category = {"pending": "todo", "in_progress": "in_progress", "review": "in_progress", "done": "done"}[lane]
    t = new_ticket(tracker="jira", id=id, url="", title=f"t {id}", description="x", status=status, status_category=category, is_subtask=subtask,
                   story_points={"value": points, "source": "f", "known": True}, sprints=sprints or [])
    return t


S = lambda id, name, state="active": {"id": id, "name": name, "state": state, "board_id": 1}   # noqa: E731


class TestLaneOnTickets:
    def test_new_ticket_carries_its_lane(self):
        assert [T("A", lane).get("lane") for lane in ("pending", "in_progress", "review", "done")] == ["pending", "in_progress", "review", "done"]

    def test_a_blocked_status_is_pending(self):
        t = new_ticket(tracker="jira", id="A", url="", title="", description="", status="Blocked", status_category="in_progress",
                       story_points={"value": 2.0, "source": "f", "known": True})
        assert t["lane"] == "pending" and t["blocked"] is True

    def test_lane_overrides_apply(self):
        t = new_ticket(tracker="jira", id="A", url="", title="", description="", status="Ready to Ship", status_category="in_progress",
                       story_points={"value": 2.0, "source": "f", "known": True}, lane_overrides={"review": ["Ready to Ship"]})
        assert t["lane"] == "review"


class TestSummary:
    def test_totals_come_from_every_ticket_including_done_ones(self):
        tickets = [T("A", "pending", 3), T("B", "done", 5), T("C", "review", None)]
        s = load.sprint_summary(tickets, target=None, rollover=None)
        assert s["lanes"]["done"]["points"] == 5.0 and s["lanes"]["review"]["unpointed"] == ["C"] and s["total"]["count"] == 3

    def test_without_a_target_sprint_carry_over_is_unknown_not_zero(self):
        s = load.sprint_summary([T("A")], target=None, rollover=None)
        assert s["carry_over"] is None and any("carry-over" in n for n in s["notes"])

    def test_carry_over_counts_work_that_was_in_earlier_sprints(self):
        target = S(30, "Sprint 30", "future")
        tickets = [T("A", "pending", 5, [S(28, "Sprint 28", "closed"), S(29, "Sprint 29", "closed"), target]),
                   T("B", "review", 3, [S(29, "Sprint 29", "closed"), target]), T("C", "done", 2, [S(29, "Sprint 29", "closed"), target]),
                   T("D", "pending", 8, [target]), T("A-1", "pending", 5, [S(29, "Sprint 29", "closed"), target], subtask=True)]
        c = load.sprint_summary(tickets, target=target, rollover=None)["carry_over"]
        assert c["count"] == 3 and c["points"] == 10.0                      # A, B, C; the sub-task and the new ticket do not count
        assert c["by_lane"] == {"pending": ["A"], "in_progress": [], "review": ["B"], "done": ["C"]}
        assert c["chronic"] == ["A"]                                          # 2+ earlier sprints and not done
        assert c["times"] == {"A": 2, "B": 1, "C": 1} and abs(c["share_of_sprint"] - 10 / 18) < 1e-9
        assert tickets[0]["carry_over"] == {"times": 2, "from_sprints": ["Sprint 28", "Sprint 29"]} and tickets[3]["carry_over"] == {"times": 0, "from_sprints": []}

    def test_later_sprints_are_not_carry_over(self):
        target = S(30, "Sprint 30")
        t = T("A", "pending", 3, [target, S(31, "Sprint 31", "future")])
        assert load.sprint_summary([t], target=target, rollover=None)["carry_over"]["count"] == 0

    def test_rollover_is_passed_through(self):
        roll = {"from_sprint": "Sprint 29", "tickets": [{"id": "X", "lane": "in_progress", "story_points": 3.0, "already_in_target": False}]}
        assert load.sprint_summary([T("A")], target=S(30, "Sprint 30", "future"), rollover=roll)["pending_rollover"] == roll


class TestFlags:
    CFG = ReadinessConfig()

    def _flags(self, tickets, capacity=None, target=None, rollover=None, cfg=None):
        s = load.sprint_summary(tickets, target=target, rollover=rollover)
        return {f["flag"]: f for f in load.flags(s, tickets, cfg or self.CFG, capacity)}

    def test_unpointed_work_is_named_in_every_lane_including_done(self):
        f = self._flags([T("A", "pending", None), T("B", "done", None)])
        assert f["unpointed"]["ids"] == ["A", "B"] and "velocity" in f["unpointed"]["ask"]

    def test_off_scale_and_too_big(self):
        f = self._flags([T("A", points=4), T("B", points=21), T("C", points=5)])
        assert f["off_scale"]["ids"] == ["A", "B"]

    def test_over_capacity_counts_only_open_work(self):
        tickets = [T("A", "pending", 20), T("B", "in_progress", 15), T("C", "review", 8), T("D", "done", 50)]
        f = self._flags(tickets, capacity=40)
        assert f["over_capacity"]["over_by"] == 3.0 and f["over_capacity"]["open_points"] == 43.0
        assert "over_capacity" not in self._flags(tickets, capacity=43) and "over_capacity" not in self._flags(tickets)

    def test_carry_over_share_above_the_threshold_is_flagged_and_says_it_is_a_heuristic(self):
        target = S(30, "Sprint 30")
        tickets = [T("A", "pending", 8, [S(29, "Sprint 29", "closed"), target]), T("B", "pending", 2, [target])]
        f = self._flags(tickets, target=target)
        assert f["carry_share"]["share"] == 0.8 and "heuristic" in f["carry_share"]["ask"]
        assert "carry_share" not in self._flags(tickets, target=target, cfg=ReadinessConfig(carry_over_share=0.9))

    def test_chronic_and_never_started_and_in_flight_carry_over(self):
        target = S(30, "Sprint 30")
        old = [S(27, "Sprint 27", "closed"), S(28, "Sprint 28", "closed")]
        tickets = [T("A", "pending", 3, old + [target]), T("B", "in_progress", 3, [S(29, "Sprint 29", "closed"), target]), T("C", "review", 3, [S(29, "Sprint 29", "closed"), target])]
        f = self._flags(tickets, target=target)
        assert f["chronic_carry_over"]["ids"] == ["A"] and f["carried_not_started"]["ids"] == ["A"] and f["carried_in_flight"]["ids"] == ["B", "C"]

    def test_hidden_rollover_lists_only_work_missing_from_the_target(self):
        roll = {"from_sprint": "Sprint 29", "tickets": [{"id": "X", "lane": "in_progress", "story_points": 3.0, "already_in_target": False},
                                                        {"id": "Y", "lane": "pending", "story_points": 2.0, "already_in_target": True}]}
        f = self._flags([T("A")], target=S(30, "Sprint 30", "future"), rollover=roll)
        assert f["hidden_rollover"]["ids"] == ["X"] and f["hidden_rollover"]["points"] == 3.0

    def test_a_clean_sprint_has_no_flags(self):
        assert load.flags(load.sprint_summary([T("A", "pending", 3)], target=None, rollover=None), [T("A")], self.CFG, None) == []


from scripts.tickets import load_report  # noqa: E402


def _sprint():
    target = S(30, "Sprint 30", "future")
    old = [S(27, "Sprint 27", "closed"), S(28, "Sprint 28", "closed")]
    tickets = [T("A", "pending", 5, old + [target]), T("B", "in_progress", 6, [S(29, "Sprint 29", "closed"), target]), T("C", "review", 22, [target]),
               T("D", "done", 5, [target]), T("E", "pending", None, [target]), T("F", "review", 11, [target])]
    roll = {"from_sprint": "Sprint 29", "tickets": [{"id": "X", "lane": "in_progress", "story_points": 3.0, "already_in_target": False}]}
    summary = load.sprint_summary(tickets, target=target, rollover=roll)
    return summary, load.flags(summary, tickets, ReadinessConfig(), 40)


class TestLoadReport:
    def test_markdown_has_a_lane_table_with_points_share_and_unpointed_ids(self):
        md = "\n".join(load_report.markdown(*_sprint(), capacity=40))
        assert "## Sprint load" in md and "| Lane | Tickets | Points | % of points | Without points |" in md
        assert "| Pending | 2 | 5 | " in md and "| Review | 2 | 33 | " in md and "| Done | 1 | 5 |" in md and "E" in md
        assert "| **Total** | 6 | 49 | " in md
        assert "Open load (pending + in progress + review): 44 pts vs capacity 40: **over by 4**" in md

    def test_markdown_lists_carry_over_by_lane_and_the_hidden_rollover(self):
        md = "\n".join(load_report.markdown(*_sprint(), capacity=None))
        assert "### Carry-over" in md and "2 ticket(s)" in md and "Chronic" in md and "A" in md
        assert "### Hidden rollover" in md and "X" in md and "Sprint 29" in md
        assert "over by" not in md      # no capacity given: no capacity claim

    def test_markdown_ends_with_the_decisions_each_flag_asks_for(self):
        md = "\n".join(load_report.markdown(*_sprint(), capacity=40))
        assert "### To decide" in md and "Which tickets leave the sprint?" in md and "Add points" in md

    def test_a_sprint_without_carry_over_data_says_so(self):
        summary = load.sprint_summary([T("A")], target=None, rollover=None)
        md = "\n".join(load_report.markdown(summary, [], capacity=None))
        assert "not computed" in md and "### Carry-over" not in md

    def test_slack_is_a_few_short_lines_without_tables(self):
        text = "\n".join(load_report.slack(*_sprint(), capacity=40))
        lines = text.splitlines()
        assert lines[0].startswith("*Sprint load*") and "pending 5 pts (2) · in progress 6 pts (1) · review 33 pts (2) · done 5 pts (1) · total 49 pts (6)" in lines[0]
        assert any("over by 4" in ln for ln in lines) and not any(ln.lstrip().startswith("|") for ln in lines) and len(text) < 1800

    def test_slack_ids_are_neutralised(self):
        summary, flags = _sprint()
        flags[0]["ids"] = ["<!channel>", "A"]
        assert "<!channel>" not in "\n".join(load_report.slack(summary, flags, capacity=None))

    def test_action_items_go_to_the_team_and_the_tech_lead(self):
        items = dict(load_report.action_items(*_sprint()))
        assert items["Team (carry-over decisions: split, re-scope or drop)"] == ["A"] and items["Tech lead (unfinished work missing from this sprint)"] == ["X"]


class TestPointsThatCouldNotBeRead:
    def _unreadable(self, id):
        t = T(id, "pending", None)
        t["story_points"] = {"value": None, "source": "field list unreadable", "known": False}
        return t

    def test_unreadable_points_are_a_flag_not_an_add_points_request_and_stop_the_capacity_verdict(self):
        tickets = [self._unreadable("A"), self._unreadable("B"), T("C", "pending", 50)]
        s = load.sprint_summary(tickets, target=None, rollover=None)
        f = {x["flag"]: x for x in load.flags(s, tickets, ReadinessConfig(), 10)}
        assert f["points_unknown"]["ids"] == ["A", "B"] and "unpointed" not in f and "over_capacity" not in f
        assert any("could not be read" in n for n in s["notes"])


def test_finished_work_is_not_flagged_off_scale():
    tickets = [T("A", "done", 21), T("B", "pending", 4)]
    f = {x["flag"]: x for x in load.flags(load.sprint_summary(tickets, None, None), tickets, ReadinessConfig(), None)}
    assert f["off_scale"]["ids"] == ["B"]
