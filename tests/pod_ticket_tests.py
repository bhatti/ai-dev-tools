"""Pod functional tests for the ticket tools (scripts/tickets), real scripts in the real image, no LLM:

    PYTHONPATH=. python3 tests/test_pod_functional.py --tests tickets-pipeline,tickets-gh-readonly,tickets-model-shell

tickets-pipeline       a fake Jira server inside the pod; fetch -> readiness -> write -> re-fetch as separate processes
tickets-gh-readonly    real `gh` against the sandbox repo: read, search by label, a missing issue, and a dry-run write that changes nothing
tickets-model-shell    the environment the model's shell gets (claude_runner.build_claude_env) can run `python3 -m scripts.*` from /workspace
"""

from __future__ import annotations

import os
from types import ModuleType

SCRIPTS = [
    "scripts/tickets/__init__.py", "scripts/tickets/config.py", "scripts/tickets/fetch.py", "scripts/tickets/github.py", "scripts/tickets/jira.py",
    "scripts/tickets/model.py", "scripts/tickets/readiness.py", "scripts/tickets/sections.py", "scripts/tickets/signals.py",
    "scripts/tickets/story_points.py", "scripts/tickets/write.py", "scripts/common/adf.py", "scripts/common/jira_api.py",
    "scripts/common/http_retry.py", "scripts/common/claude_runner.py", "scripts/common/report_bundle.py", "scripts/common/redact.py",
    "scripts/common/pr_signals/github.py", "scripts/common/pr_signals/bitbucket.py",
    "tests/__init__.py", "tests/fakes/__init__.py", "tests/fakes/jira.py", "tests/fakes/ticket_story.py",
]
GH_REPO = f"{os.environ.get('GH_ORG', 'bhatti')}/{os.environ.get('GH_REPO', 'todo-sample')}"
GH_ISSUE = os.environ.get("QA_TEST_GH_ISSUE", "2")
GH_LABEL = os.environ.get("QA_TEST_GH_LABEL", "ai-pr-open")

PIPELINE = r'''
set -u
cd /app; W=/workspace/tickets_pipeline; rm -rf $W; mkdir -p $W/out
cat > $W/driver.py <<'PY'
import json, os, subprocess, sys, threading
from tests.fakes.jira import FakeJira
from tests.fakes import ticket_story

class Env:  # the fake only needs setenv
    def setenv(self, k, v): os.environ[k] = v

W = "/workspace/tickets_pipeline"
fake = FakeJira(Env())
ticket_story.load_jira(fake)
threading.Thread(target=fake.server.serve_forever, daemon=True).start()
env = {**os.environ, "DEFAULT_TRACKER": "jira", "JIRA_PROJECT": "PROJ"}

def run(module, *args):
    return subprocess.run([sys.executable, "-m", module, *args], capture_output=True, text=True, env=env, cwd="/app")

def mark(k, v): print(f"::add-task-context {k}::{v}", flush=True)

r = run("scripts.tickets.fetch", "--sprint", "Sprint 1", "--tracker", "jira", "--out", f"{W}/out/tickets.json")
mark("FETCH_RC", r.returncode)
tickets = json.load(open(f"{W}/out/tickets.json"))["tickets"]
mark("FETCHED", ",".join(t["id"] for t in tickets))
mark("TEXT_FILES", ",".join(sorted(os.listdir(f"{W}/out/tickets"))))
grades = {"tickets": [
  {"id": "PROJ-1", "checks": {"why": "pass", "scope": "pass", "acs": "pass", "dod": "pass", "variants": "na"}},
  {"id": "PROJ-2", "checks": {"why": "pass", "scope": "missing", "acs": "partial", "dod": "pass", "variants": "na"}},
  {"id": "PROJ-3", "checks": {"why": "missing", "scope": "missing", "acs": "missing", "dod": "missing", "variants": "na"}},
  {"id": "PROJ-4", "checks": {"why": "partial", "scope": "missing", "acs": "missing", "dod": "missing", "variants": "na"}}]}
json.dump(grades, open(f"{W}/out/grades.json", "w"))
r = run("scripts.tickets.readiness", "--tickets", f"{W}/out/tickets.json", "--grades", f"{W}/out/grades.json", "--out-dir", f"{W}/reports", "--dry-run")
mark("READINESS_RC", r.returncode)
res = json.load(open(f"{W}/reports/result.json"))
mark("STATUSES", ",".join(f"{t['id']}={t['status']}" for t in res["tickets"]))
mark("SIGNAL", json.loads(r.stdout.strip().splitlines()[-1])["signal"])
slack = open(f"{W}/reports/slack.md").read()
mark("REPORT_FILES", ",".join(sorted(n for n in ("report.md", "report.html", "result.json", "slack.md") if os.path.exists(f"{W}/reports/{n}"))))
mark("SLACK_SHORT_NO_TABLES", len(slack) < 3500 and not any(ln.lstrip().startswith("|") for ln in slack.splitlines()))
open(f"{W}/out/acs.md", "w").write("- Given 10k users, when I search, then results appear within 2s. Test: make perf\n")
before = fake.description_text("PROJ-2")
dry = run("scripts.tickets.write", "--ref", "PROJ-2", "--kind", "acs", "--section-file", f"{W}/out/acs.md", "--tracker", "jira")
mark("DRY_ACTION", json.loads(dry.stdout.strip().splitlines()[-1])["action"])
mark("DRY_UNCHANGED", fake.description_text("PROJ-2") == before)
applied = run("scripts.tickets.write", "--ref", "PROJ-2", "--kind", "acs", "--section-file", f"{W}/out/acs.md", "--tracker", "jira", "--apply")
mark("APPLY_ACTION", json.loads(applied.stdout.strip().splitlines()[-1])["action"])
after = fake.description_text("PROJ-2")
mark("KEPT_ORIGINAL", before.strip() in after)
again = run("scripts.tickets.write", "--ref", "PROJ-2", "--kind", "acs", "--section-file", f"{W}/out/acs.md", "--tracker", "jira", "--apply")
mark("SECOND_ACTION", json.loads(again.stdout.strip().splitlines()[-1])["action"])
mark("SECTIONS", fake.description_text("PROJ-2").count("Acceptance Criteria (generated)"))
blocked = run("scripts.tickets.write", "--ref", "PROJ-3", "--kind", "acs", "--section-file", f"{W}/out/acs.md", "--tracker", "jira", "--apply")
mark("UNPOINTED_RC", blocked.returncode)
PY
python3 $W/driver.py
'''

GH_READONLY = r'''
cd /app; W=/workspace/tickets_gh; rm -rf $W; mkdir -p $W
python3 -m scripts.tickets.fetch __ISSUE__ 99999 --repo __REPO__ --tracker github --out $W/tickets.json > /dev/null; echo "::add-task-context FETCH_RC::$?"
python3 -m scripts.tickets.fetch --label __LABEL__ --repo __REPO__ --tracker github --out $W/by-label.json > /dev/null; echo "::add-task-context LABEL_RC::$?"
python3 - <<'PY'
import json
W = "/workspace/tickets_gh"
t = json.load(open(W + "/tickets.json"))["tickets"]
print("::add-task-context FIRST_ID::" + t[0]["id"])
print("::add-task-context FIRST_HAS_TEXT::" + str(bool(t[0]["title"]) and t[0]["fetch_error"] == ""))
print("::add-task-context MISSING_UNREADABLE::" + str(t[1]["fetch_error"] != "" and t[1]["story_points"]["known"] is False))
print("::add-task-context LABEL_COUNT_OK::" + str(json.load(open(W + "/by-label.json"))["count"] >= 1))
PY
echo "Generated by the pod test." > $W/plan.md
python3 -m scripts.tickets.write --ref __ISSUE__ --repo __REPO__ --tracker github --kind plan --section-file $W/plan.md > $W/dry.json; echo "::add-task-context DRY_RC::$?"
python3 -c "import json; print('::add-task-context DRY_OK::' + str(json.load(open('$W/dry.json'))['action'] in ('dry_run', 'exists', 'ineligible')))"
python3 -m scripts.tickets.fetch __ISSUE__ --repo __REPO__ --tracker github --out $W/again.json > /dev/null
python3 -c "import json; a=json.load(open('$W/tickets.json'))['tickets'][0]['description']; b=json.load(open('$W/again.json'))['tickets'][0]['description']; print('::add-task-context UNCHANGED::' + str(a == b))"
'''

MODEL_SHELL = r'''
cd /workspace && python3 - <<'PY'
import subprocess
from scripts.common.claude_runner import build_claude_env
env = build_claude_env()
r = subprocess.run(["bash", "-c", "cd /workspace && python3 -m scripts.tickets.readiness --help"], env=env, capture_output=True, text=True)
print("::add-task-context MODEL_SHELL_RC::" + str(r.returncode))
print("::add-task-context MODEL_SHELL_USAGE::" + str("usage:" in r.stdout))
PY
'''


def build_tests(h: ModuleType) -> dict:
    for rel in SCRIPTS:
        if rel not in h._SCRIPTS_TO_COPY:
            h._SCRIPTS_TO_COPY.append(rel)

    def test_pipeline(base_env):
        result = h.TestResult("tickets-pipeline")
        with h.pod_fixture("tickets-pipeline") as pod:
            step = h.exec_step(pod, "pipeline", PIPELINE, dict(base_env), timeout=240)
            result.steps.append(step)
            err = h._check_values(step, {
                "FETCH_RC": "0", "FETCHED": "PROJ-1,PROJ-2,PROJ-3,PROJ-4", "TEXT_FILES": "PROJ-1.txt,PROJ-2.txt,PROJ-3.txt,PROJ-4.txt", "READINESS_RC": "0", "SIGNAL": "DONE_WITH_CONCERNS",
                "STATUSES": "PROJ-1=READY,PROJ-2=NEEDS_AC_CLARIFICATION,PROJ-3=NEEDS_AC_WORK,PROJ-4=BLOCKED",
                "REPORT_FILES": "report.html,report.md,result.json,slack.md", "SLACK_SHORT_NO_TABLES": "True",
                "DRY_ACTION": "dry_run", "DRY_UNCHANGED": "True", "APPLY_ACTION": "appended", "KEPT_ORIGINAL": "True",
                "SECOND_ACTION": "exists", "SECTIONS": "1", "UNPOINTED_RC": "3"})
            if err:
                return h._fail(result, step, err)
        return h._pass(result, "fake Jira in the pod: fetch, verdicts, dry run changes nothing, apply appends once and keeps the original, a re-run does nothing, unpointed tickets are refused")

    def test_gh_readonly(base_env):
        result = h.TestResult("tickets-gh-readonly")
        env = {k: v for k, v in base_env.items() if not k.startswith(("JIRA_", "BITBUCKET_"))}
        env.update({"DEFAULT_TRACKER": "github", "GH_ORG": GH_REPO.split("/")[0], "GH_REPO": GH_REPO.split("/")[1]})
        script = GH_READONLY.replace("__ISSUE__", GH_ISSUE).replace("__REPO__", GH_REPO).replace("__LABEL__", GH_LABEL)
        with h.pod_fixture("tickets-gh-readonly") as pod:
            step = h.exec_step(pod, "gh", script, env, timeout=240)
            result.steps.append(step)
            err = h._check_values(step, {
                "FETCH_RC": "0", "LABEL_RC": "0", "FIRST_ID": f"#{GH_ISSUE}", "FIRST_HAS_TEXT": "True", "MISSING_UNREADABLE": "True",
                "LABEL_COUNT_OK": "True", "DRY_RC": "0", "DRY_OK": "True", "UNCHANGED": "True"})
            if err:
                return h._fail(result, step, err)
        return h._pass(result, f"real gh on {GH_REPO}: issue read, label search, missing issue kept as unreadable, dry-run write changed nothing")

    def test_model_shell(base_env):
        result = h.TestResult("tickets-model-shell")
        with h.pod_fixture("tickets-model-shell") as pod:
            step = h.exec_step(pod, "model-shell", MODEL_SHELL, dict(base_env), timeout=120)
            result.steps.append(step)
            err = h._check_values(step, {"MODEL_SHELL_RC": "0", "MODEL_SHELL_USAGE": "True"})
            if err:
                return h._fail(result, step, err)
        return h._pass(result, "the model's shell environment can run python3 -m scripts.* from /workspace")

    return {"tickets-pipeline": test_pipeline, "tickets-gh-readonly": test_gh_readonly, "tickets-model-shell": test_model_shell}
