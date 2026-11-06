"""Tests for scripts/analyze/changelog_report.py: merging the model's changelog with deterministic facts, sanitising, rendering."""

import json

from scripts.analyze import changelog_report as cr


def sig(kind="field_removed", confidence="high", file="proto/pay.proto", evidence="string currency = 3"):
    return {"kind": kind, "file": file, "evidence": evidence, "confidence": confidence, "detail": ""}


def rec(num, teams=("payments",), level="low", signals=(), internal=(), external=False, title=None):
    return {"number": num, "title": title or f"PR {num}", "url": f"https://x/{num}", "author": "dev", "state": "merged", "merged_at": "2026-01-02", "files": 1, "loc": 5,
            "files_truncated": False, "classes": {}, "areas": [], "teams": list(teams), "owners": [], "owner_source": "codeowners",
            "blast": {"level": level, "external": external, "internal": list(internal), "reasons": ["r"]}, "signals": list(signals), "consumers": [], "diff_read": True,
            "labels": []}


def facts(*records):
    teams = {}
    for r in records:
        for t in r["teams"]:
            s = teams.setdefault(t, {"prs": 0, "high_blast": 0, "breaking_candidates": 0})
            s["prs"] += 1
            s["high_blast"] += r["blast"]["level"] == "high"
            s["breaking_candidates"] += any(x["confidence"] in ("high", "medium") for x in r["signals"])
    return {"prs": list(records), "teams": teams, "breaking": [], "meta": {"prs": len(records), "diff_prs": len(records), "diff_limit": 25, "diff_errors": [],
                                                                               "codeowners_rules": 3, "files_truncated_prs": 0, "note": "n"}}


def write(tmp_path, data):
    (tmp_path / "changelog_model.json").write_text(json.dumps(data) if not isinstance(data, str) else data)


class TestClassify:
    def test_conventional_prefix_labels_and_wording(self):
        c = cr.classify_change
        assert c("feat(ui): add dark mode") == "feature" and c("ABC-12: fix(api): crash on empty body") == "fix"
        assert c("chore: bump deps") == "maintenance" and c("Bump lodash from 1 to 2") == "maintenance"
        assert c("Fix crash when the queue is empty") == "fix" and c("Rotate signing keys", ["security"]) == "security"
        assert c("Rework the importer") == "change"

    def test_plain_title_drops_prefix_and_ticket(self):
        assert cr.plain_title("feat(ui): add dark mode") == "add dark mode"
        assert cr.plain_title("ABC-12: Rework importer") == "Rework importer"
        assert cr.plain_title("Rework importer") == "Rework importer"


class TestLoad:
    def test_missing_or_invalid_json_gives_none(self, tmp_path):
        assert cr.load_model(tmp_path) is None
        write(tmp_path, "{bad")
        assert cr.load_model(tmp_path) is None
        write(tmp_path, ["x"])
        assert cr.load_model(tmp_path) is None

    def test_malformed_items_are_dropped_or_coerced(self, tmp_path):
        write(tmp_path, {"themes": ["x", {"title": 5, "items": [{"pr": "7", "type": "bogus", "summary": " s "}, "no", {"pr": None}],
                                          "summary": 3}, {"summary": "untitled"}],
                         "breaking": ["no", {"pr": 1, "kind": "k", "confidence": "bogus", "consumers": "web"}],
                         "tests": [{"team": "t"}, {"team": "a", "what": "w"}]})
        m = cr.load_model(tmp_path)
        assert [t["title"] for t in m["themes"]] == ["5"]
        assert m["themes"][0]["items"] == [{"pr": 7, "type": "", "summary": "s", "verify": ""}]
        assert m["breaking"][0]["confidence"] == "low" and m["breaking"][0]["consumers"] == ["web"]
        assert m["tests"] == [{"team": "a", "what": "w"}]


class TestMerge:
    def test_every_pr_is_listed_with_a_link_even_without_a_model(self):
        out = cr.merge(facts(rec(1, title="feat: add export"), rec(2, title="fix: crash"), rec(3, title="chore: bump x")), None)
        items = [i for t in out["themes"] for i in t["items"]]
        assert out["mode"] == "facts" and sorted(i["pr"] for i in items) == [1, 2] and items[0]["url"].startswith("https://x/")
        assert [m["pr"] for m in out["maintenance"]] == [3] and out["summary"]["changes"] == 2

    def test_a_bot_authored_feature_is_still_a_change_not_a_chore(self):
        r = rec(1, title="PROJ-1: Add agent policies")
        r["automated"] = True
        out = cr.merge(facts(r), None)
        assert out["maintenance"] == [] and out["summary"]["changes"] == 1

    def test_model_themes_come_first_and_leftovers_are_grouped_by_team(self):
        m = {"themes": [{"title": "Export", "summary": "s", "items": [{"pr": 1, "type": "feature", "summary": "CSV export", "verify": "open a file"}, {"pr": 99}]}]}
        out = cr.merge(facts(rec(1, teams=("data",)), rec(2, teams=("web",), title="fix: x")), m)
        assert [t["title"] for t in out["themes"]] == ["Export", "More changes: web"]
        assert out["themes"][0]["items"][0]["summary"] == "CSV export" and out["themes"][0]["items"][0]["verify"] == "open a file"
        assert out["mode"] == "model"

    def test_a_pr_is_placed_once(self):
        m = {"themes": [{"title": "A", "items": [{"pr": 1}]}, {"title": "B", "items": [{"pr": 1}]}]}
        out = cr.merge(facts(rec(1)), m)
        assert [t["title"] for t in out["themes"]] == ["A"]

    def test_signals_become_plain_language_rows_and_repeated_evidence_collapses(self):
        sigs = [sig("required_added", "high", "x.json", f"required property p{i}") for i in range(3)]
        out = cr.merge(facts(rec(1, signals=sigs, internal=["web"], title="feat: secrets")), None)
        (b,) = out["breaking_changes"]
        assert b["confidence"] == "high" and "New required input" in b["what"] and "p0" in b["what"] and "p2" in b["evidence"]
        assert b["consumers"] == ["web"] and b["url"] == "https://x/1" and b["migration"]

    def test_model_summary_replaces_the_template_and_adds_semantic_rows(self):
        m = {"themes": [], "breaking": [{"pr": 1, "kind": "field_removed", "summary": "Mobile 3.x loses currency", "confidence": "high"},
                                          {"pr": 1, "kind": "semantic", "confidence": "medium", "summary": "Totals now round half-up", "migration": "re-run totals"}]}
        out = cr.merge(facts(rec(1, signals=[sig()])), m)
        assert sorted(b["what"] for b in out["breaking_changes"]) == ["Mobile 3.x loses currency", "Totals now round half-up"]

    def test_unknown_pr_in_model_breaking_is_dropped(self):
        out = cr.merge(facts(rec(1)), {"themes": [], "breaking": [{"pr": 99, "kind": "semantic", "summary": "ghost"}]})
        assert out["breaking_changes"] == []

    def test_low_confidence_path_candidates_are_counted_not_listed(self):
        out = cr.merge(facts(rec(1, signals=[sig("contract_changed", "low", "", "proto/a.proto")])), None)
        assert out["breaking_changes"] == [] and out["summary"]["candidates_low"] == 1

    def test_rollup_is_sorted_by_confidence(self):
        out = cr.merge(facts(rec(1, signals=[sig("db_drop", "medium", "m.sql", "DROP COLUMN x")]), rec(2, signals=[sig()])), None)
        assert [b["confidence"] for b in out["breaking_changes"]] == ["high", "medium"] and out["summary"]["breaking_high"] == 1

    def test_team_rows_list_their_own_notable_changes_not_a_placeholder(self):
        out = cr.merge(facts(rec(1, level="high", title="feat: rotate keys"), rec(2, level="low", title="fix: typo in label")), None)
        changes = out["teams"]["payments"]["changes"]
        assert [c["pr"] for c in changes] == [1, 2] and changes[0]["summary"] == "rotate keys" and changes[0]["url"] == "https://x/1"

    def test_a_team_with_only_chores_still_shows_what_changed(self):
        out = cr.merge(facts(rec(1, level="high", title="chore: bump deps")), None)
        assert [c["summary"] for c in out["teams"]["payments"]["changes"]] == ["bump deps"]

    def test_tests_are_suggested_per_signal_kind_and_per_consumer_team(self):
        out = cr.merge(facts(rec(1, signals=[sig()], internal=["web"])), None)
        assert any(t["team"] == "web" and "contract" in t["what"].lower() for t in out["tests"])


class TestRender:
    def _out(self):
        f = facts(rec(1, level="high", signals=[sig()], internal=["web"], external=True, title="Pay API v2"), rec(2, teams=("web",), level="medium", title="fix: polish"),
                  rec(3, title="chore: bump y"))
        return cr.merge(f, {"themes": [{"title": "Payments", "summary": "Adds currency <b>x</b> | y",
                                        "items": [{"pr": 1, "type": "feature", "summary": "Pay API v2 | new", "verify": "replay payloads"}]}]})

    def test_changelog_and_blast_radius_are_separate_sections(self):
        md = cr.render_markdown(self._out())
        assert md.startswith("## Changelog & Blast Radius")
        for h in ("### Changelog", "### Breaking-change candidates", "### Blast radius by team", "### Suggested tests", "### How this was computed"):
            assert h in md
        assert md.index("### Changelog") < md.index("### Breaking-change candidates") < md.index("### Blast radius by team")
        assert "Other notable changes" not in md

    def test_items_show_type_summary_link_team_and_verify(self):
        md = cr.render_markdown(self._out())
        assert "- **New**: Pay API v2 \\| new ([#1](https://x/1)) · payments · blast high" in md
        assert "Verify: replay payloads" in md and "#### Maintenance" in md and "[#3](https://x/3)" in md

    def test_breaking_table_explains_what_may_break_with_a_linked_pr(self):
        md = cr.render_markdown(self._out())
        row = next(l for l in md.splitlines() if l.startswith("| high |"))
        assert "field was removed" in row and "[#1](https://x/1)" in row and "web" in row and "field_removed" not in row

    def test_evidence_is_escaped_exactly_once(self):
        out = cr.merge(facts(rec(1, signals=[sig("required_added", "high", "x.json", "required property a|b")])), None)
        assert "a|b" in out["breaking_changes"][0]["what"]   # the data stays raw
        row = next(l for l in cr.render_markdown(out).splitlines() if l.startswith("| high |"))
        assert "a\\|b" in row and "a\\\\|b" not in row

    def test_untrusted_text_is_escaped(self):
        md = cr.render_markdown(self._out())
        assert "<b>" not in md and "\\<b\\>" in md

    def test_no_prs_and_no_breaking_say_so(self):
        out = cr.merge(facts(), None)
        assert "No merged PRs" in cr.render_markdown(out)
        assert "None found" in cr.render_markdown(cr.merge(facts(rec(1)), None))

    def test_digest_line(self):
        line = cr.digest_line(self._out())
        assert "1 breaking-change candidate (1 high confidence)" in line and "PR #1" in line and "field was removed" in line
        assert cr.digest_line(cr.merge(facts(), None)) == ""

    def test_write_outputs_creates_json_and_the_two_separate_documents(self, tmp_path):
        section = cr.write_outputs(self._out(), tmp_path, "Acme")
        assert json.loads((tmp_path / "changelog.json").read_text())["summary"]["breaking_high"] == 1
        changelog, blast = (tmp_path / "changelog.md").read_text(), (tmp_path / "blast_radius.md").read_text()
        assert "Breaking-change candidates" in changelog and "Blast radius" not in changelog
        assert "Blast radius by team" in blast and "Breaking-change candidates" not in blast
        assert "<h2>" in (tmp_path / "changelog.html").read_text() and (tmp_path / "blast_radius.html").exists()
        assert section.startswith("## Changelog & Blast Radius")
