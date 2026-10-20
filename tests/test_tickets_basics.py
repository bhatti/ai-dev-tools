"""Tests for the small pure modules of scripts/tickets: sections, signals, config, story points."""

from __future__ import annotations

import pytest

from scripts.common import jira_api
from scripts.common.jira_api import JiraError
from scripts.tickets import config as cfg
from scripts.tickets import signals as sg
from scripts.tickets import story_points as sp
from scripts.tickets.sections import SECTIONS, append_marked, contains_markers, has_marked, has_plan, replace_marked, split_marked, with_heading, wrap
from tests.fakes.jira import FakeJira

ACS = SECTIONS["acs"]


class TestSections:
    def test_wrap_adds_the_heading_once_and_the_markers(self):
        assert wrap(ACS, "- one") == "<!-- ygs:acceptance-criteria:start -->\n## Acceptance Criteria (generated)\n- one\n<!-- ygs:acceptance-criteria:end -->"
        assert with_heading(ACS, "## Acceptance Criteria (generated)\n- one").count("## ") == 1

    def test_append_keeps_the_body_and_replace_swaps_only_the_block(self):
        body = append_marked("Why\nbecause", ACS, "- old")
        assert body.startswith("Why\nbecause") and has_marked(body, ACS)
        body = append_marked(body, SECTIONS["plan"], "- plan")
        out = replace_marked(body, ACS, "- new")
        assert "- new" in out and "- old" not in out and "- plan" in out and out.startswith("Why\nbecause")

    def test_replacing_a_missing_block_raises(self):
        with pytest.raises(KeyError):
            replace_marked("nothing", ACS, "- x")

    @pytest.mark.parametrize("text", ["<!-- ygs:impl-plan:start -->\nx\n<!-- ygs:impl-plan:end -->", "<!-- ai-plan:start -->\n## Implementation Plan\nold\n<!-- ai-plan:end -->",
                                      "## Implementation Plan\nsteps", "text\n### implementation plan (draft)\n"])
    def test_a_plan_is_detected_in_every_form_it_can_take(self, text):
        assert has_plan(text)

    def test_ordinary_text_is_not_a_plan(self):
        assert not has_plan("We need an implementation plan someday.\n## Notes")


class TestMarkedBlocks:
    def test_an_orphan_start_marker_is_not_paired_with_our_end_marker(self):
        body = "intro\n<!-- ygs:acceptance-criteria:start -->\nUSER\n"
        assert not has_marked(body, ACS)
        out = append_marked(body, ACS, "- ours")
        outside, inside = split_marked(out, ACS)
        assert "USER" in outside and "- ours" in inside and "USER" not in inside

    def test_the_heading_is_replaced_not_repeated_when_the_text_already_has_one(self):
        for first in ("## Implementation Plan", "## Implementation Plan (generated)", "# implementation plan", "### Implementation Plan"):
            out = with_heading(SECTIONS["plan"], f"{first}\n- step")
            assert out.splitlines()[0] == "## Implementation Plan (generated)" and out.count("mplementation") == 1
        assert with_heading(SECTIONS["plan"], "- step\n## Risks").startswith("## Implementation Plan (generated)\n- step")

    def test_text_that_holds_a_marker_is_detected(self):
        assert contains_markers("a <!-- ygs:impl-plan:end --> b") and not contains_markers("plain text")


class TestSignals:
    def test_alternative_spec_headings_count_as_evidence(self):
        sig = sg.signals("## Why\nx\n## Outcome\nUser sees a toast\n## Verification\nrun the suite")
        assert sg.has_behavior_evidence(sig) and sig["spec_headers"] == ["outcome", "verification"] and sig["has_dod"]

    def test_given_when_then_and_test_lines_are_counted(self):
        sig = sg.signals("- Given a cart\n- When I add\n- Then it shows\n- Test: make test")
        assert sig["given_when_then_lines"] == 3 and sig["test_lines"] == 1

    def test_plain_prose_is_not_behavior_evidence(self):
        assert not sg.has_behavior_evidence(sg.signals("Make the page faster."))


class TestConfig:
    def test_defaults_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        c = cfg.load_config()
        assert c.points_scale == (1, 2, 3, 5, 8, 13) and c.variants == () and "In Review" in c.no_enrich_statuses

    def test_reads_a_file(self, tmp_path):
        f = tmp_path / "r.yml"
        f.write_text("points_scale: [1, 2, 4]\nvariants:\n  - {name: single, description: one process}\nno_enrich_statuses: [Done]\n")
        c = cfg.load_config(f)
        assert c.points_scale == (1.0, 2.0, 4.0) and c.variants[0]["name"] == "single" and c.no_enrich_statuses == ("Done",)

    @pytest.mark.parametrize("text", ["points_scale: []", "variants: [1]", "no_enrich_statuses: 3", "- a list"])
    def test_malformed_files_raise(self, tmp_path, text):
        f = tmp_path / "r.yml"
        f.write_text(text)
        with pytest.raises(ValueError):
            cfg.load_config(f)

    def test_the_repository_checkout_is_searched_when_the_working_directory_has_no_file(self, tmp_path, monkeypatch):
        work, repo = tmp_path / "work", tmp_path / "repo"
        (repo / ".ygs").mkdir(parents=True)
        (repo / ".ygs" / "readiness.yml").write_text("points_scale: [1, 2]\n")
        work.mkdir()
        monkeypatch.chdir(work)
        monkeypatch.setenv("CODEBASE_DIR", str(repo))
        assert cfg.load_config().points_scale == (1.0, 2.0)
        monkeypatch.delenv("CODEBASE_DIR")
        assert cfg.load_config().points_scale == cfg.DEFAULT_SCALE

    def test_an_explicitly_named_missing_file_raises(self, tmp_path):
        with pytest.raises(ValueError):
            cfg.load_config(tmp_path / "missing.yml")


class TestStoryPoints:
    @pytest.fixture()
    def jira(self, monkeypatch):
        jira_api._field_id_cache.clear()
        with FakeJira(monkeypatch) as fake:
            yield fake

    def _cfg(self, jira):
        return {"JIRA_BASE_URL": jira.base, "JIRA_EMAIL": "t@example.com", "JIRA_API_TOKEN": "x"}

    def test_fields_are_found_by_name_not_id(self, jira):
        jira.add_field("customfield_99", "Story Points (Fibonacci Sequence)").add_field("customfield_5", "Epic Link")
        assert sp.jira_point_fields(self._cfg(jira)) == [("customfield_10016", "Story point estimate"), ("customfield_99", "Story Points (Fibonacci Sequence)")]

    def test_the_override_selects_fields_by_name_or_id(self, jira):
        cfg_ = {**self._cfg(jira), "JIRA_STORY_POINT_FIELDS": "customfield_10020"}
        assert sp.jira_point_fields(cfg_) == [("customfield_10020", "Sprint")]

    def test_a_set_value_wins_and_option_fields_work(self):
        fields = [("a", "Story Points"), ("b", "Story point estimate")]
        assert sp.jira_points({"a": None, "b": 5}, fields) == {"value": 5.0, "source": "Story point estimate", "known": True}
        assert sp.jira_points({"a": {"value": "8"}}, fields)["value"] == 8.0

    def test_null_everywhere_is_unset_but_known(self):
        r = sp.jira_points({"a": None}, [("a", "Story Points")])
        assert r["value"] is None and r["known"] is True

    def test_an_unreadable_field_list_is_unknown_never_unset(self):
        assert sp.jira_points({}, None)["known"] is False

    def test_a_site_without_a_points_field_is_unknown(self):
        assert sp.jira_points({}, [])["known"] is False

    def test_field_list_errors_propagate(self, jira):
        jira.fail("/rest/api/3/field", 401)
        with pytest.raises(JiraError):
            sp.jira_point_fields(self._cfg(jira))

    @pytest.mark.parametrize("label,value", [("sp:3", 3.0), ("points: 5", 5.0), ("Story-Points/8", 8.0), ("estimate-2", 2.0), ("story_points=13", 13.0)])
    def test_github_labels(self, label, value):
        assert sp.github_points(["bug", label])["value"] == value

    def test_github_without_a_label_is_unset_and_known(self):
        assert sp.github_points(["bug"]) == {"value": None, "source": "labels", "known": True}

    def test_a_project_value_wins_over_a_label_and_an_unreadable_project_is_unknown(self):
        assert sp.github_points(["sp:1"], project_value=5.0)["value"] == 5.0
        assert sp.github_points([], project_known=False)["known"] is False
