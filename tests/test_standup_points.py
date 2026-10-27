"""Story points in the standup: lanes and totals from the signals, in the board tables and the synthesis prompt."""

from datetime import date, datetime, timedelta, timezone

from scripts.standup import gather_gh, gather_jira, points, post, render_html, synthesize

FIELDS = [("customfield_10016", "Story Points")]


def jira_raw(key, status, category, pts, assignee="Ann", subtask=False, board=1):
    return {"key": key, "id": "1", "_board_id": board, "fields": {
        "summary": key, "status": {"name": status, "statusCategory": {"key": category}}, "assignee": {"displayName": assignee, "accountId": "a"},
        "updated": datetime.now(timezone.utc).isoformat(), "labels": [], "priority": {"name": "High"}, "issuetype": {"subtask": subtask},
        "customfield_10016": pts}}


def norm(raw, fields=FIELDS):
    return gather_jira._normalise_issue(raw, datetime.now(timezone.utc) - timedelta(days=2), {"JIRA_BASE_URL": "https://x.atlassian.net"}, 26, fields)


def signals(raws, fields=FIELDS, sprints=None):
    issues = [norm(r, fields) for r in raws]
    return {"issues": issues, "all_sprints": sprints if sprints is not None else [{"id": 7, "board_id": 1, "board": "Core", "name": "Sprint 7", "end_date": "2099-01-01T00:00:00Z"}],
            "points_known": points.points_known(issues)}


RAWS = [jira_raw("A-1", "To Do", "new", 3), jira_raw("A-2", "In Progress", "indeterminate", 5), jira_raw("A-3", "In Review", "indeterminate", 8, "Bob"),
        jira_raw("A-4", "Done", "done", 2, "Bob"), jira_raw("A-5", "Blocked", "indeterminate", 1), jira_raw("A-6", "To Do", "new", 99, subtask=True),
        jira_raw("A-7", "In Progress", "indeterminate", None, "Bob")]


class TestJiraIssues:
    def test_every_issue_carries_its_lane_and_points(self):
        got = {i["key"]: (i["lane"], i["story_points"]) for i in signals(RAWS)["issues"]}
        assert got == {"A-1": ("pending", 3.0), "A-2": ("in_progress", 5.0), "A-3": ("review", 8.0), "A-4": ("done", 2.0), "A-5": ("pending", 1.0),
                       "A-6": ("pending", 99.0), "A-7": ("in_progress", None)}

    def test_totals_per_lane_leave_subtasks_out_and_count_the_unpointed(self):
        s = signals(RAWS)
        (row,) = points.board_rows(s, date.today())
        assert row["cells"] == {"pending": "4 pts (2)", "in_progress": "5 pts (2)", "review": "8 pts (1)", "done": "2 pts (1)"} and row["total"] == "19 pts (6)"
        assert points.unpointed_note(s) == "1 issue(s) have no story points and count as 0 pts."

    def test_per_person_totals(self):
        people = {r["person"]: r for r in points.people_rows(signals(RAWS))}
        assert people["Ann"]["cells"]["pending"] == "4 pts (2)" and people["Bob"]["cells"]["review"] == "8 pts (1)" and people["Bob"]["cells"]["done"] == "2 pts (1)"

    def test_unreadable_point_fields_are_unknown_not_zero(self):
        s = signals(RAWS, fields=None)
        assert s["points_known"] is False and points.unpointed_note(s).startswith("Story points could not be read")
        assert "points unavailable" in points.prompt_block(s)

    def test_the_fields_requested_include_the_point_field_and_issue_type(self):
        assert points.jira_fields(FIELDS) == ",customfield_10016,issuetype"


class TestGitHubIssues:
    def raw(self, labels, number=1):
        return {"number": number, "title": "t", "labels": [{"name": n} for n in labels], "assignees": [{"login": "ann"}], "updatedAt": "2026-01-01T00:00:00Z", "comments": []}

    def norm(self, labels, closed=False):
        return gather_gh._normalise_issue(self.raw(labels), datetime.now(timezone.utc) - timedelta(days=2), closed=closed)

    def test_lane_comes_from_state_and_labels_and_points_from_a_label(self):
        assert (self.norm(["sp:3"])["lane"], self.norm(["sp:3"])["story_points"]) == ("pending", 3.0)
        assert self.norm(["in progress"])["lane"] == "in_progress" and self.norm(["in review"])["lane"] == "review"
        done = self.norm(["sp:5"], closed=True)
        assert done["lane"] == "done" and done["status"] == "closed" and done["story_points"] == 5.0


class TestRendering:
    def test_markdown_board_has_a_column_per_lane_with_points_and_counts(self):
        md = post._board_status_md(signals(RAWS))
        assert "| Board | Sprint | Pending | In progress | Review | Done | Total | Days Left |" in md
        assert "| Core | Sprint 7 | 4 pts (2) | 5 pts (2) | 8 pts (1) | 2 pts (1) | 19 pts (6) |" in md
        assert "| Bob | 0 pts (0) | 0 pts (1) | 8 pts (1) | 2 pts (1) | 10 pts (3) |" in md  # busiest first among the open load
        assert "1 issue(s) have no story points" in md

    def test_html_board_and_people_tables(self):
        s = signals(RAWS)
        html = render_html._board_status_rows(s) + render_html._people_section(s)
        assert "4 pts (2)" in html and "8 pts (1)" in html and "Points by person" in html and "have no story points" in html

    def test_issues_without_a_board_are_not_counted_in_every_sprint(self):
        two = [{"id": 7, "board_id": 1, "board": "A", "name": "S1", "end_date": ""}, {"id": 8, "board_id": 2, "board": "B", "name": "S2", "end_date": ""}]
        s = signals(RAWS + [jira_raw("B-1", "Done", "done", 13, board=2), jira_raw("N-1", "Done", "done", 40, board=None)], sprints=two)
        rows = {r["sprint"]: r for r in points.board_rows(s, date.today())}
        assert rows["S1"]["cells"]["done"] == "2 pts (1)" and rows["S2"]["cells"]["done"] == "13 pts (1)"
        assert sum(float(r["cells"]["done"].split()[0]) for r in points.people_rows(s)) == 55.0

    def test_github_finished_work_reaches_the_tables_and_the_prompt(self):
        gh = TestGitHubIssues()
        open_ = [gh.norm(["sp:5", "in progress"])]
        done = [gh.norm(["sp:8"], closed=True)]
        s = {"tracker": "github", "issues": open_, "done_issues": done, "all_sprints": []}
        md = post._board_status_md(s)
        assert "| github | Current scope | 0 pts (0) | 5 pts (1) | 0 pts (0) | 8 pts (1) | 13 pts (2) |" in md
        assert "8 pts (1)" in render_html._board_status_rows(s)
        assert "done 8 pts (1)" in points.prompt_block(s)

    def test_signals_from_before_lanes_existed_still_render(self):
        old = {"issues": [{"key": "A-1", "status": "In Progress", "assignee": "Ann"}], "all_sprints": [{"id": 1, "board_id": None, "board": "B", "name": "S", "end_date": ""}]}
        assert "0 pts (1)" in post._board_status_md(old)


class TestPrompt:
    def test_prompt_carries_computed_totals(self):
        prompt = synthesize._build_prompt(signals(RAWS), skill_md=None)
        assert "STORY POINTS" in prompt and "pending 4 pts (2) · in progress 5 pts (2) · review 8 pts (1) · done 2 pts (1) · total 19 pts (6)" in prompt
        assert "Bob: " in prompt
