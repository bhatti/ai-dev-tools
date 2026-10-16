"""Tests for scripts/common/pr_metrics.py: the single registry behind every PR table."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from scripts.common import pr_metrics as pm

SIG = {"groups": ["comments", "reviewers", "tasks", "commits"], "truncated": [], "error": "",
       "comments_human": 4, "comments_bot": 2, "major_comments_human": 1, "major_comments_bot": 2,
       "reviewers": [{"name": "bob", "is_bot": False, "approved": True, "sources": ["review"]},
                     {"name": "coderabbitai[bot]", "is_bot": True, "approved": False, "sources": ["comment"]}],
       "reviewers_human": 1, "reviewers_bot": 1, "approvers": ["bob"],
       "tasks_human": 3, "tasks_bot": 1, "tasks_unresolved": 2, "commits": 6}


def pr(**kw):
    base = {"number": 12, "url": "https://x/pull/12", "title": "Add | pipe", "category": "api", "pr_type": "feature",
            "blast_radius": "medium", "risk_score": 22, "risk_tier": "medium", "total_loc": 1200, "file_count": 9,
            "ci_status": "pass", "age_hours": 72, "signals": SIG, "ai_findings": {"major": 2, "critical": 1, "high": 1}}
    base.update(kw)
    return base


def cells(line: str) -> list[str]:
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


class TestPerPrTable:
    def test_default_columns_include_every_new_metric(self):
        lines = pm.build_pr_table([pr()])
        header = cells(lines[0])
        assert header == [pm.REGISTRY[c].header for c in pm.TABLE_COLUMNS]
        row = dict(zip(header, cells(lines[2])))
        assert row["Comments"] == "4 👤 · 2 🤖"
        assert row["Tasks"] == "3 👤 · 1 🤖 (2 open)"
        assert row["Major/Critical"] == "5 (ai 2 · bot 2 · human 1)"
        assert row["Commits"] == "6"
        assert row["Reviewers"] == "1/1 ✅ · 1 🤖"
        assert row["Title"] == "Add \\| pipe"

    def test_unfetched_signals_are_dashes_and_failed_ones_are_na(self):
        no_sig = dict(zip([pm.REGISTRY[c].header for c in pm.TABLE_COLUMNS], cells(pm.build_pr_table([pr(signals=None, ai_findings=None)])[2])))
        assert (no_sig["Comments"], no_sig["Tasks"], no_sig["Commits"], no_sig["Major/Critical"]) == ("—", "—", "—", "—")
        failed = dict(zip([pm.REGISTRY[c].header for c in pm.TABLE_COLUMNS], cells(pm.build_pr_table([pr(signals={"error": "rate limit", "groups": []})])[2])))
        assert (failed["Comments"], failed["Tasks"], failed["Commits"]) == ("n/a", "n/a", "n/a")

    def test_truncated_counts_get_a_plus(self):
        sig = {**SIG, "truncated": ["comments", "commits"]}
        row = dict(zip([pm.REGISTRY[c].header for c in pm.TABLE_COLUMNS], cells(pm.build_pr_table([pr(signals=sig)])[2])))
        assert row["Comments"].endswith("+") and row["Commits"] == "6+"

    def test_legacy_reviewer_counts_still_render_without_signals(self):
        row = dict(zip([pm.REGISTRY[c].header for c in pm.TABLE_COLUMNS],
                       cells(pm.build_pr_table([pr(signals=None, approval_count=1, reviewer_count=3)])[2])))
        assert row["Reviewers"] == "1/3 ✅"

    def test_ci_unavailable_and_index_and_stacked_columns(self):
        lines = pm.build_pr_table([pr()], pm.STACKED_COLUMNS, index=True, ci_unavailable=True)
        header = cells(lines[0])
        assert header[0] == "#" and "Target" in header and "Cx" in header
        assert dict(zip(header, cells(lines[2])))["CI"] == "N/A"

    def test_pr_number_comes_from_the_url_for_queue_rows(self):
        assert pm._c_pr({"id": 1, "url": "https://x/pull/42"}, {}) == "[#42](https://x/pull/42)"

    def test_every_column_id_is_registered(self):
        for cols in (pm.TABLE_COLUMNS, pm.STACKED_COLUMNS, pm.QUEUE_COLUMNS, pm.STANDUP_COLUMNS):
            assert all(c in pm.REGISTRY for c in cols)


class TestSinglePrTable:
    def test_one_table_with_scope_size_risk_and_review_activity(self):
        p = pr(scope="billing", scope_description="Billing only", additions=1000, deletions=200, categories=["backend"],
               owners=["@team"], sensitive_paths=["auth/x.py"], created_at="2026-09-25T10:00:00Z")
        lines = pm.build_single_pr_metrics(p)
        assert cells(lines[0]) == ["Metric", "Value", "Benchmark", "Signal", "Description"]
        metrics = {cells(l)[0]: cells(l) for l in lines[2:]}
        for name in ("Scope", "Blast Radius", "LOC Changed", "Files Changed", "Risk Score", "File Categories", "Owners",
                     "Sensitive Paths", "PR Age", "Commits", "Reviewers", "Comments (human · bot)", "Tasks (human · bot)",
                     "Major/Critical Issues"):
            assert name in metrics, name
        assert metrics["LOC Changed"][1] == "1200 (+1000/−200)" and metrics["Scope"][4] == "Billing only"
        assert metrics["Major/Critical Issues"][3] == "🔴" and metrics["Commits"][3] == "🟡"

    def test_rows_without_data_are_left_out(self):
        lines = pm.build_single_pr_metrics({"total_loc": 5, "file_count": 1, "blast_radius": "low"}, ("loc", "category", "owners", "age"))
        assert [cells(l)[0] for l in lines[2:]] == ["LOC Changed"]

    @pytest.mark.parametrize("sig,expected", [
        ({**SIG, "comments_human": 0, "comments_bot": 3}, "🟡"),
        ({**SIG, "comments_human": 0, "comments_bot": 0}, "🔴"),
        ({**SIG, "comments_human": 2}, "🟢"),
    ])
    def test_comment_signal(self, sig, expected):
        row = next(cells(l) for l in pm.build_single_pr_metrics(pr(signals=sig), ("comments",))[2:])
        assert row[3] == expected


class TestSignalsPlumbing:
    def test_attach_signals_matches_number_id_and_pr_number(self):
        prs = [{"number": 1}, {"id": 2}, {"pr_number": "3"}, {"title": "no number"}]
        pm.attach_signals(prs, {1: {"x": 1}, 2: {"x": 2}, 3: {"x": 3}}, {2: {"major": 1}})
        assert [p.get("signals", {}).get("x") for p in prs] == [1, 2, 3, None] and prs[1]["ai_findings"] == {"major": 1}

    def test_needs_follow_the_columns_shown(self):
        assert pm.needs_for(["pr", "title"]) == set()
        assert pm.needs_for(pm.STANDUP_COLUMNS) == {"reviewers", "comments", "tasks"}
        assert pm.needs_for(pm.TABLE_COLUMNS) == {"reviewers", "comments", "tasks", "commits"}

    def test_enrich_never_raises_and_skips_when_nothing_is_needed(self, capsys, monkeypatch):
        monkeypatch.delenv("PR_SIGNALS", raising=False)
        prs = [{"number": 1}]
        pm.enrich_with_signals(prs, "github", {}, columns=("pr",))       # no signal columns: nothing to fetch
        pm.enrich_with_signals(prs, "github", {})                          # no GH_ORG/GH_REPO: error is contained
        assert "signals" not in prs[0] and "review signals unavailable" in capsys.readouterr().err

    def test_aggregate_rows_only_count_prs_with_signals(self):
        prs = [pr(), pr(signals={**SIG, "comments_human": 0}), pr(signals=None)]
        rows = {r[0]: r for r in pm.aggregate_rows(prs)}
        assert rows["PRs without a human comment"][1] == "1/2 (50%)"
        assert rows["Review comments (human · bot)"][1] == "4 · 4"
        assert rows["Review tasks (open/total)"][1] == "4/8"
        assert rows["Avg commits per PR"][1] == "6.0"
        assert pm.aggregate_rows([pr(signals=None)]) == []


ROOT = Path(os.environ.get("YGS_SKILLS_DIR") or Path(__file__).resolve().parents[2] / "you-got-skills")


@pytest.mark.skipif(not (ROOT / "skills" / "shared" / "pr-metrics.md").exists(), reason="you-got-skills checkout not found")
def test_every_review_metric_is_a_row_of_the_shared_skill_definition_table():
    """The skill-side definition table must name each metric exactly as the code labels it (first column of a table row)."""
    rows = {line.split("|")[1].strip() for line in (ROOT / "skills" / "shared" / "pr-metrics.md").read_text().splitlines() if line.startswith("| ")}
    for metric_id in ("comments", "tasks", "major", "reviewers", "commits"):
        label = pm.REGISTRY[metric_id].label
        assert label in rows, f"{metric_id}: '{label}' is not a row of the definition table in shared/pr-metrics.md"
