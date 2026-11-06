"""Tests for scripts/analyze/changelog_report.py: merging the model's changelog with deterministic facts, sanitising, rendering."""

import json

from scripts.analyze import changelog_report as cr


def sig(kind="field_removed", confidence="high", file="proto/pay.proto", evidence="string currency = 3"):
    return {"kind": kind, "file": file, "evidence": evidence, "confidence": confidence, "detail": ""}


def rec(num, teams=("payments",), level="low", signals=(), internal=(), external=False, title=None):
    return {"number": num, "title": title or f"PR {num}", "url": f"https://x/{num}", "author": "dev", "state": "merged", "merged_at": "2026-01-02", "files": 1, "loc": 5,
            "files_truncated": False, "classes": {}, "areas": [], "teams": list(teams), "owners": [], "owner_source": "codeowners",
            "blast": {"level": level, "external": external, "internal": list(internal), "reasons": ["r"]}, "signals": list(signals), "consumers": [], "diff_read": True}


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


class TestLoad:
    def test_missing_or_invalid_json_gives_none(self, tmp_path):
        assert cr.load_model(tmp_path) is None
        write(tmp_path, "{bad")
        assert cr.load_model(tmp_path) is None
        write(tmp_path, ["x"])
        assert cr.load_model(tmp_path) is None

    def test_malformed_items_are_dropped_or_coerced(self, tmp_path):
        write(tmp_path, {"features": ["x", {"title": 5, "prs": "7", "breaking": ["no", {"kind": "k", "confidence": "bogus", "consumers": "web"}],
                                            "tests": [{"team": "t"}, {"team": "a", "what": "w"}]}, {"title": "ok", "prs": [1, "2", None, "x"]}]})
        m = cr.load_model(tmp_path)
        assert [f["title"] for f in m["features"]] == ["5", "ok"]
        assert m["features"][0]["prs"] == [7] and m["features"][1]["prs"] == [1, 2]
        assert m["features"][0]["breaking"][0]["confidence"] == "low" and m["features"][0]["breaking"][0]["consumers"] == ["web"]
        assert m["features"][0]["tests"] == [{"team": "a", "what": "w"}]


class TestMerge:
    def test_blast_and_teams_come_from_facts_and_the_model_cannot_lower_them(self):
        f = facts(rec(1, level="high", teams=("identity",)), rec(2, level="low", teams=("payments",)))
        out = cr.merge(f, {"features": [{"title": "Login rework", "summary": "s", "prs": [1, 2], "blast": {"level": "low"}}]})
        feat = out["features"][0]
        assert feat["blast"]["level"] == "high" and feat["teams"] == ["identity", "payments"]

    def test_unknown_prs_are_dropped_and_empty_features_removed(self):
        out = cr.merge(facts(rec(1)), {"features": [{"title": "Ghost", "prs": [99]}, {"title": "Real", "prs": [1, 99]}]})
        assert [(f["title"], f["prs"]) for f in out["features"] if f["title"] != cr.OTHER] == [("Real", [1])]

    def test_deterministic_breaking_signals_are_attached_even_if_the_model_missed_them(self):
        out = cr.merge(facts(rec(1, signals=[sig()], external=True, internal=["web"])), {"features": [{"title": "Pay API", "prs": [1]}]})
        b = out["features"][0]["breaking"]
        assert [(x["kind"], x["confidence"], x["source"]) for x in b] == [("field_removed", "high", "signal")] and b[0]["consumers"] == ["web"]

    def test_low_confidence_path_candidates_stay_out_of_the_feature_but_in_the_rollup_count(self):
        out = cr.merge(facts(rec(1, signals=[sig("contract_changed", "low", "", "proto/a.proto")])), {"features": [{"title": "F", "prs": [1]}]})
        assert out["features"][0]["breaking"] == [] and out["summary"]["candidates_low"] == 1

    def test_model_breaking_items_are_kept_and_merged_without_duplicates(self):
        m = {"features": [{"title": "F", "prs": [1], "breaking": [{"kind": "field_removed", "confidence": "high", "evidence": "string currency = 3", "pr": 1},
                                                                   {"kind": "semantic", "confidence": "medium", "evidence": "rounding changed", "migration": "re-run totals", "pr": 1}]}]}
        out = cr.merge(facts(rec(1, signals=[sig()])), m)
        assert sorted(x["kind"] for x in out["features"][0]["breaking"]) == ["field_removed", "semantic"]

    def test_significant_prs_not_covered_by_any_feature_land_in_other_notable_changes(self):
        out = cr.merge(facts(rec(1), rec(2, level="high", title="Rotate keys")), {"features": [{"title": "F", "prs": [1]}]})
        other = [f for f in out["features"] if f["title"] == cr.OTHER]
        assert other and other[0]["prs"] == [2]

    def test_tests_are_suggested_per_signal_kind_and_per_consumer_team(self):
        out = cr.merge(facts(rec(1, signals=[sig()], internal=["web"])), {"features": [{"title": "F", "prs": [1]}]})
        tests = out["features"][0]["tests"]
        assert any(t["team"] == "web" and "contract" in t["what"].lower() for t in tests)

    def test_without_a_model_changelog_features_are_built_from_facts_alone(self):
        out = cr.merge(facts(rec(1, level="high", title="Rotate keys"), rec(2, level="low")), None)
        assert out["mode"] == "facts" and [f["title"] for f in out["features"]] == ["Rotate keys"]

    def test_rollup_is_sorted_by_confidence_and_counted(self):
        f = facts(rec(1, signals=[sig("db_drop", "medium", "m.sql", "DROP COLUMN x")]), rec(2, signals=[sig()]))
        out = cr.merge(f, {"features": [{"title": "A", "prs": [1]}, {"title": "B", "prs": [2]}]})
        assert [b["confidence"] for b in out["breaking_changes"]] == ["high", "medium"] and out["summary"]["breaking_high"] == 1


class TestRender:
    def _out(self):
        f = facts(rec(1, level="high", signals=[sig()], internal=["web"], external=True, title="Pay API v2"), rec(2, teams=("web",), level="medium"))
        return cr.merge(f, {"features": [{"title": "Pay API v2", "summary": "Adds currency <b>x</b> | y", "prs": [1]}, {"title": "Checkout polish", "prs": [2]}]})

    def test_sections_and_content(self):
        md = cr.render_markdown(self._out())
        assert md.startswith("## Changelog & Blast Radius")
        for h in ("### Breaking-change candidates", "### Blast radius by team", "### Features by team", "### Suggested tests", "### How this was computed"):
            assert h in md
        assert "field_removed" in md and "payments" in md and "web" in md and "review aid" in md.lower()

    def test_untrusted_text_is_escaped(self):
        md = cr.render_markdown(self._out())
        assert "<b>" not in md and "\\<b\\>" in md

    def test_empty_changelog_says_so(self):
        out = cr.merge(facts(rec(1)), None)
        assert "No notable changes" in cr.render_markdown(out)

    def test_digest_line(self):
        line = cr.digest_line(self._out())
        assert "1 breaking-change candidate (1 high confidence)" in line and "field_removed in PR #1" in line
        assert cr.digest_line(cr.merge(facts(rec(1)), None)) == ""

    def test_write_outputs_creates_json_md_and_html(self, tmp_path):
        cr.write_outputs(self._out(), tmp_path, "Acme")
        assert json.loads((tmp_path / "changelog.json").read_text())["summary"]["breaking_high"] == 1
        assert (tmp_path / "changelog.md").read_text().startswith("## Changelog") and "<h2>" in (tmp_path / "changelog.html").read_text()
