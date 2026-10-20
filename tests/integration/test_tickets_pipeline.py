"""The ticket tools end to end, the way the skills run them: real CLI entry points in subprocesses, once against a Jira
server and once against `gh`, both serving the same backlog (tests/fakes/ticket_story.py)."""

from __future__ import annotations

import json
import os

import pytest

from scripts.common import jira_api
from tests.fakes import ticket_story
from tests.fakes.gh_issues import FakeGhIssues
from tests.fakes.jira import FakeJira
from tests.integration.pipeline import base_env, run_module, write_json

ACS = "- Given 10k users, when I search, then results appear within 2s. Test: make perf\n- Given an empty query, when I search, then no request is sent. Test: make ui\n- Given a failing backend, when I search, then an error toast shows. Test: make err\n"
PLAN = "**Approach:** add an index on the users table\n- `db/users.sql` - add the index\n"


@pytest.fixture(params=["jira", "github"])
def backlog(request, tmp_path, monkeypatch):
    jira_api._field_id_cache.clear()
    if request.param == "jira":
        with FakeJira(monkeypatch) as fake:
            ticket_story.load_jira(fake)
            env = base_env(tmp_path, "jira", {k: os.environ[k] for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")} | {"JIRA_PROJECT": "PROJ"})
            yield request.param, fake, env, ["--sprint", "Sprint 1", "--tracker", "jira"], {n: f"PROJ-{n}" for n in range(1, 5)}
    else:
        monkeypatch.setenv("GH_ORG", "acme")
        monkeypatch.setenv("GH_REPO", "widgets")
        fake = FakeGhIssues(tmp_path, monkeypatch)
        ticket_story.load_github(fake)
        env = base_env(tmp_path, "github")
        env["PATH"] = os.environ["PATH"]
        yield request.param, fake, env, ["--milestone", "Sprint 1", "--tracker", "github"], {n: f"#{n}" for n in range(1, 5)}


def description(kind, fake, n):
    return fake.description_text(f"PROJ-{n}") if kind == "jira" else fake.body(n)


def test_fetch_grade_report_enrich_and_re_evaluate(tmp_path, backlog):
    kind, fake, env, scope, ids = backlog
    out = tmp_path / "ready-out"
    reports = tmp_path / "reports"

    # 1. fetch
    r = run_module("scripts.tickets.fetch", env, *scope, "--out", str(out / "tickets.json"))
    assert r.returncode == 0, r.stdout + r.stderr
    fetched = json.loads((out / "tickets.json").read_text())
    assert [t["id"] for t in fetched["tickets"]] == [ids[n] for n in (1, 2, 3, 4)]

    # 2. the model's grades (ticket 2 keeps its spec under Outcome/Verification: partial, not missing)
    write_json(out / "grades.json", {"tickets": [
        {"id": ids[1], "checks": {"why": "pass", "scope": "pass", "acs": "pass", "dod": "pass", "variants": "na"}},
        {"id": ids[2], "checks": {"why": "pass", "scope": "missing", "acs": "partial", "dod": "pass", "variants": "na"},
         "says": "Results for 10k users appear in under 2 seconds", "questions": ["Which fields does search match?"], "ac_starter": "Given 10k users, when I search, then results show in 2s"},
        {"id": ids[3], "checks": {"why": "missing", "scope": "missing", "acs": "missing", "dod": "missing", "variants": "na"}},
        {"id": ids[4], "checks": {"why": "partial", "scope": "missing", "acs": "missing", "dod": "missing", "variants": "na"}}]})

    # 3. verdicts
    r = run_module("scripts.tickets.readiness", env, "--tickets", str(out / "tickets.json"), "--grades", str(out / "grades.json"), "--out-dir", str(reports), "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    result = json.loads((reports / "result.json").read_text())
    status = {t["id"]: t["status"] for t in result["tickets"]}
    assert status == {ids[1]: "READY", ids[2]: "NEEDS_AC_CLARIFICATION", ids[3]: "NEEDS_AC_WORK", ids[4]: "BLOCKED"}
    assert json.loads(r.stdout.strip().splitlines()[-1])["signal"] == "DONE_WITH_CONCERNS"

    # 4. enrich the clarifiable ticket: ACs, then plan; text preserved; a second run changes nothing
    before = description(kind, fake, 2)
    (out / "acs.md").write_text(ACS)
    (out / "plan.md").write_text(PLAN)
    actions = []
    for kind_, file in (("acs", "acs.md"), ("plan", "plan.md")):
        w = run_module("scripts.tickets.write", env, "--ref", ids[2], "--kind", kind_, "--section-file", str(out / file), "--apply", *scope[-2:])
        assert w.returncode == 0, w.stdout + w.stderr
        actions.append(json.loads(w.stdout.strip().splitlines()[-1]))
    assert [a["action"] for a in actions] == ["appended", "appended"] and all(a["verified"] for a in actions)
    after = description(kind, fake, 2)
    assert before.strip() in after and "Results for 10k users appear in under 2 seconds" in after and "results appear within 2s" in after
    again = run_module("scripts.tickets.write", env, "--ref", ids[2], "--kind", "acs", "--section-file", str(out / "acs.md"), "--apply", *scope[-2:])
    assert json.loads(again.stdout.strip().splitlines()[-1])["action"] == "exists" and description(kind, fake, 2) == after

    # 5. re-fetch sees the plan on either tracker; the actions turn the ticket into ENRICHED (it has points; the scope gap stays listed)
    run_module("scripts.tickets.fetch", env, *scope, "--out", str(out / "tickets.json"))
    refreshed = {t["id"]: t for t in json.loads((out / "tickets.json").read_text())["tickets"]}
    assert refreshed[ids[2]]["has_impl_plan"] is True
    write_json(out / "actions.json", actions)
    r = run_module("scripts.tickets.readiness", env, "--tickets", str(out / "tickets.json"), "--grades", str(out / "grades.json"),
                   "--actions", str(out / "actions.json"), "--out-dir", str(reports))
    final = {t["id"]: t["status"] for t in json.loads((reports / "result.json").read_text())["tickets"]}
    assert final[ids[2]] == "ENRICHED"


def test_a_dry_run_chain_changes_nothing(tmp_path, backlog):
    kind, fake, env, scope, ids = backlog
    (tmp_path / "acs.md").write_text(ACS)
    before = description(kind, fake, 2)
    w = run_module("scripts.tickets.write", env, "--ref", ids[2], "--kind", "acs", "--section-file", str(tmp_path / "acs.md"), *scope[-2:])
    assert w.returncode == 0 and json.loads(w.stdout.strip().splitlines()[-1])["action"] == "dry_run"
    assert description(kind, fake, 2) == before


def test_the_points_gate_stops_unpointed_tickets(tmp_path, backlog):
    kind, fake, env, scope, ids = backlog
    (tmp_path / "acs.md").write_text(ACS)
    w = run_module("scripts.tickets.write", env, "--ref", ids[3], "--kind", "acs", "--section-file", str(tmp_path / "acs.md"), "--apply", *scope[-2:])
    assert w.returncode == 3 and json.loads(w.stdout.strip().splitlines()[-1])["action"] == "blocked_points"
    assert description(kind, fake, 3).strip() == ticket_story.PROSE
