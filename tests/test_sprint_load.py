"""Tests for scripts/common/sprint_load.py: the one lane model and story-point totals shared by sprint-ready and standup."""

from __future__ import annotations

import json

import pytest

from scripts.common import sprint_load as sl


class TestLaneOf:
    @pytest.mark.parametrize("status,category,lane", [
        ("To Do", "new", "pending"), ("Open", "todo", "pending"), ("Untriaged", "new", "pending"), ("Re-Opened", "new", "pending"),
        ("In Progress", "indeterminate", "in_progress"), ("Development", "in_progress", "in_progress"),
        ("In Review", "indeterminate", "review"), ("Code Complete", "indeterminate", "review"), ("QA", "in_progress", "review"),
        ("Ready for Testing", "indeterminate", "review"), ("Verified", "indeterminate", "review"),
        ("Done", "done", "done"), ("Closed", "done", "done"), ("Verified", "done", "done"),
    ])
    def test_the_category_decides_and_the_name_splits_review_from_in_progress(self, status, category, lane):
        assert sl.lane_of(status, category) == lane

    def test_blocked_work_is_pending_whatever_the_workflow_calls_its_category(self):
        assert sl.lane_of("Blocked", "indeterminate") == "pending" and sl.lane_of("Blocked", "new") == "pending"

    def test_a_missing_category_falls_back_to_the_name(self):
        assert sl.lane_of("Done", "") == "done" and sl.lane_of("In Review", "") == "review" and sl.lane_of("Whatever", "") == "pending"

    def test_overrides_win_for_the_names_they_list(self):
        overrides = {"review": ["Ready to Ship"], "pending": ["On Hold"]}
        assert sl.lane_of("ready to ship", "indeterminate", overrides) == "review"
        assert sl.lane_of("On Hold", "indeterminate", overrides) == "pending"
        assert sl.lane_of("In Progress", "indeterminate", overrides) == "in_progress"

    def test_overrides_come_from_the_environment_and_bad_ones_are_rejected(self):
        assert sl.overrides_from_env({"SPRINT_LANES": json.dumps({"review": ["QA Ready"]})}) == {"review": ["QA Ready"]}
        assert sl.overrides_from_env({}) == {}
        for bad in ("not json", json.dumps({"nonsense": ["x"]}), json.dumps({"review": "QA"}), json.dumps([1])):
            with pytest.raises(ValueError):
                sl.overrides_from_env({"SPRINT_LANES": bad})


def item(id, lane, points=None, subtask=False):
    return {"id": id, "lane": lane, "story_points": points, "is_subtask": subtask}


class TestSummarize:
    def test_points_and_counts_per_lane_with_unpointed_named_in_every_lane(self):
        out = sl.summarize([item("A", "pending", 3), item("B", "pending"), item("C", "in_progress", 5), item("D", "review", 8), item("E", "done", 2), item("F", "done")])
        assert out["lanes"]["pending"] == {"count": 2, "points": 3.0, "unpointed": ["B"]}
        assert out["lanes"]["done"] == {"count": 2, "points": 2.0, "unpointed": ["F"]}   # unpointed done work undercounts velocity: it is named too
        assert out["total"] == {"count": 6, "points": 18.0, "unpointed": 2}
        assert out["open"] == {"count": 4, "points": 16.0}

    def test_sub_tasks_are_left_out_because_their_points_live_on_the_parent(self):
        out = sl.summarize([item("A", "pending", 3), item("A-1", "pending", 5, subtask=True)])
        assert out["total"]["points"] == 3.0 and out["total"]["count"] == 1

    def test_shares_add_up_and_an_empty_sprint_does_not_divide_by_zero(self):
        out = sl.summarize([item("A", "pending", 1), item("B", "done", 3)])
        assert out["share"] == {"pending": 0.25, "in_progress": 0.0, "review": 0.0, "done": 0.75}
        assert sl.summarize([])["share"] == {lane: 0.0 for lane in sl.LANES} and sl.summarize([])["total"]["points"] == 0

    def test_fractional_points_are_kept(self):
        assert sl.summarize([item("A", "pending", 0.5), item("B", "pending", 2.5)])["lanes"]["pending"]["points"] == 3.0

    def test_zero_points_count_as_unpointed(self):
        assert sl.summarize([item("A", "pending", 0)])["lanes"]["pending"]["unpointed"] == ["A"]

    def test_by_person(self):
        out = sl.by_person([{**item("A", "pending", 3), "assignee": "alex"}, {**item("B", "review", 5), "assignee": "alex"},
                            {**item("C", "done"), "assignee": "sam"}, {**item("D", "pending", 1), "assignee": None}])
        assert out["alex"]["lanes"]["review"] == {"count": 1, "points": 5.0, "unpointed": []} and out["alex"]["total"]["points"] == 8.0
        assert out["sam"]["lanes"]["done"]["unpointed"] == ["C"] and out["unassigned"]["total"]["points"] == 1.0

    def test_format_line_is_the_one_line_summary_used_by_reports(self):
        out = sl.summarize([item("A", "pending", 10), item("B", "in_progress", 11), item("C", "review", 22), item("D", "done", 5)])
        assert sl.format_line(out) == "pending 10 pts (1) · in progress 11 pts (1) · review 22 pts (1) · done 5 pts (1) · total 48 pts (4)"
