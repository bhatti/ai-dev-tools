#!/usr/bin/env python3
"""Functional tests for ai-dev-tools workflows.

Submits real jobs to Formicary, waits for completion, downloads the task ZIP
artifact, and verifies that expected output files exist inside it.

Uses the haiku model by default to keep tests fast and cheap.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRE-TEST SETUP (run once before testing or after any code change)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Step 1 — Rebuild and push the Docker image (ai-dev-tools):
    cd ~/workplace/ai-dev-tools
    make docker-build
    # Builds multi-arch (linux/amd64,linux/arm64) and pushes plexobject/ai-dev-tools:latest
    # Ant worker pods use image_pull_policy: Always — next job picks up the new image automatically

Step 2 — Deploy updated workflow YAMLs to Formicary:
    cd ~/workplace/formicary/docs/examples
    source ~/.zshrc
    ./deploy-ai-jira-workflows.sh --set-configs
    # Uploads all ai-*.yaml job definitions and org configs (non-secret)
    # Also deploy standup workflows if needed:
    # ./deploy-ai-standup-jira.sh --set-configs

Step 3 — (Optional) Restart Formicary queen if configs changed:
    EC2_IP=$EC2_IP ~/workplace/formicary/scripts/deploy-formicary.sh --restart
    # Restarts the formicary queen deployment on k3s
    # NOT needed for ant workers — they are ephemeral pods, always pull latest image

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # Run default first 2 tests (jira-query + analyze):
    source ~/.zshrc && python3 tests/test_functional_workflows.py

    # Run specific tests by name:
    python3 tests/test_functional_workflows.py --tests jira-query,analyze
    python3 tests/test_functional_workflows.py --tests review
    python3 tests/test_functional_workflows.py --tests risks,prs
    python3 tests/test_functional_workflows.py --tests standup

    # Run all tests 1 at a time (default):
    python3 tests/test_functional_workflows.py --tests all --parallel 1

    # List available test names:
    python3 tests/test_functional_workflows.py --list

    # Override server:
    EC2_IP=<your-ec2-ip> python3 tests/test_functional_workflows.py

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ENV VARS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    EC2_IP            EC2 host IP — REQUIRED (no default, never hardcoded)
    FORMICARY_URL     Override full URL (default: https://{EC2_IP}.nip.io)
    FORMICARY_TOKEN   Bearer token — reads from ~/.zshrc (REQUIRED)
    FORMICARY_TLS_VERIFY  Set to "true" if you have a valid cert (default: false)
    PR_URL            Pull request URL for review/pr-comments tests (REQUIRED for those tests)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ALL OUTPUT FILES GO TO reports/ DIRECTORY (inside the task ZIP artifact)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    reports/result.json   — structured JSON result
    reports/report.md     — Markdown report
    reports/report.html   — HTML report (viewable in browser via Formicary UI)

    Every workflow writes to reports/ so YAML artifact lists never need updating.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRON JOB TESTING — MANDATORY RULE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    Cron-based jobs (e.g. ai-standup-jira) MUST be tested via trigger_cron_job(),
    NOT by submitting a fresh job request. Formicary keeps a PENDING scheduled slot
    for every cron job; the trigger API fires that slot immediately with the test
    params injected. Submitting fresh races with the live cron schedule and causes
    duplicate / conflicting executions.

    In TestCase, set `cron=True` for any job with a cron_trigger in its YAML.
    trigger_cron_job() uses: POST /api/v1/jobs/requests/:id/trigger
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import zipfile

from scripts.common.config import MODEL_BEDROCK_HAIKU
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests


def _load_zshrc() -> None:
    """Source ~/.zshrc in a real zsh process and import any new env vars.

    Using a subprocess is more reliable than regex-parsing the file — it
    handles variable substitutions, sourced sub-files, and conditional blocks
    that a regex cannot.  Vars already set in the process environment take
    precedence (so CI overrides still work).
    """
    zshrc = Path.home() / ".zshrc"
    if not zshrc.exists():
        return
    try:
        import subprocess as _sp
        result = _sp.run(
            ["zsh", "-c", f"source {zshrc} 2>/dev/null && env -0"],
            capture_output=True, text=True, timeout=15,
        )
        for entry in result.stdout.split("\0"):
            if "=" not in entry:
                continue
            k, _, v = entry.partition("=")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass  # best-effort; missing vars will surface as clear error messages below


_load_zshrc()


# ── Config ─────────────────────────────────────────────────────────────────

EC2_IP = os.environ.get("EC2_IP", "")
FORMICARY_URL = os.environ.get("FORMICARY_URL", f"https://{EC2_IP}.nip.io" if EC2_IP else "")
TOKEN = os.environ.get("FORMICARY_TOKEN", "")
TLS_VERIFY = os.environ.get("FORMICARY_TLS_VERIFY", "false").lower() not in ("false", "0", "no")

HAIKU = MODEL_BEDROCK_HAIKU
# Only override the complexity-tier models used by scripts for task routing.
# AnthropicSonnetModel/OpusModel/HaikuModel are display-only tier labels in the UI
# and must not be overridden (they would show all tiers as haiku, which is misleading).
HAIKU_OVERRIDES = {
    "AnthropicComplexityLowModel": HAIKU,
    "AnthropicComplexityHighModel": HAIKU,
}

POLL_INTERVAL = 10   # seconds between status checks
# PR_URL used for review/pr-comments tests — set via PR_URL env var
PR_URL = os.environ.get("PR_URL", "")
# ISSUE_ID: Jira issue key for analyze tests (e.g. PROJ-123) — set via env var, no hardcoded defaults
ISSUE_ID = os.environ.get("ISSUE_ID", "")
# GitHub org/repo for gh-query/gh-analyze tests — loaded from ~/.zshrc via _load_zshrc()
GH_ORG = os.environ.get("GH_ORG", "bhatti")
GH_REPO = os.environ.get("GH_REPO", "todo-sample")


# ── Test case definitions ───────────────────────────────────────────────────

@dataclass
class TestCase:
    name: str
    job_type: str
    params: dict
    task_type: str                  # which task ZIP to inspect
    expected_files: list[str]       # files that must exist inside the ZIP
    timeout: int = 600
    requires: list[str] = field(default_factory=list)  # env vars that must be set; skip if missing
    required_context_keys: list[str] = field(default_factory=list)  # task context keys that must be present after completion
    cron: bool = False              # if True, trigger existing PENDING cron job instead of submitting

    def id(self) -> str:
        return self.name.lower().replace(" ", "-").replace("/", "-")


ALL_TESTS: list[TestCase] = [
    TestCase(
        name="jira-query",
        job_type="ai-jira-query",
        params={"Query": "flaky tests", **HAIKU_OVERRIDES},
        task_type="query",
        # All output goes to reports/ directory
        expected_files=["reports/result.json", "reports/report.md", "reports/report.html"],
        timeout=300,
    ),
    TestCase(
        name="analyze",
        job_type="ai-jira-query",
        params={"Query": f"give tldr for {ISSUE_ID}", "Mode": "analyze", **HAIKU_OVERRIDES} if ISSUE_ID
              else {"Query": "flaky tests", "Mode": "analyze", **HAIKU_OVERRIDES},
        task_type="query",
        expected_files=["reports/result.json", "reports/report.md", "reports/report.html"],
        timeout=600,
        required_context_keys=["SELECTED_MODEL", "SELECTED_TRACKER", "ISSUE_COUNT",
                                "GIT_ARCHAEOLOGY", "ANALYSIS_TYPE"],
    ),
    TestCase(
        name="jira-analyze",
        job_type="ai-jira-query",
        params={"Query": f"give tldr for {ISSUE_ID}", "Mode": "analyze", **HAIKU_OVERRIDES},
        task_type="query",
        expected_files=["reports/result.json", "reports/report.md", "reports/report.html"],
        timeout=600,
        requires=["ISSUE_ID"],
        required_context_keys=["SELECTED_MODEL", "SELECTED_TRACKER", "ISSUE_COUNT", "ANALYSIS_TYPE"],
    ),
    TestCase(
        name="gh-query",
        job_type="ai-jira-query",
        params={"Query": "open issues", "DefaultTracker": "github", "GitHubOrg": GH_ORG, "GitHubRepo": GH_REPO, **HAIKU_OVERRIDES},
        task_type="query",
        expected_files=["reports/result.json", "reports/report.md", "reports/report.html"],
        timeout=300,
    ),
    TestCase(
        name="gh-analyze",
        job_type="ai-jira-query",
        params={"Query": "open issues", "Mode": "analyze", "DefaultTracker": "github", "GitHubOrg": GH_ORG, "GitHubRepo": GH_REPO, **HAIKU_OVERRIDES},
        task_type="query",
        expected_files=["reports/result.json", "reports/report.md", "reports/report.html"],
        timeout=600,
        required_context_keys=["SELECTED_MODEL", "SELECTED_TRACKER", "ISSUE_COUNT",
                                "GIT_ARCHAEOLOGY", "ANALYSIS_TYPE"],
    ),
    TestCase(
        name="review",
        # 15 turns: enough to fetch PR diff + write findings.json — validates end-to-end pipeline
        job_type="ai-jira-review",
        params={"PRUrl": PR_URL, "MaxTurnsReview": "8", **HAIKU_OVERRIDES},
        task_type="review",
        expected_files=["findings.json", "reports/report.md", "reports/report.html", "reports/findings.json"],
        timeout=1200,
        requires=["PR_URL"],
        required_context_keys=["SKILL", "SKILL_LOADED", "YGS_SKILLS_COUNT",
                                "YGS_SKILLS_INSTALLED", "YGS_SKILLS_REPO_COMMIT", "SKILLS_INVOKED"],
    ),
    TestCase(
        name="risks",
        # 15 turns with haiku: validates adhoc pipeline without running for 20+ minutes
        job_type="ai-adhoc",
        params={"Prompt": "risk scan", "Skill": "ygs-risk-scan", "MaxTurnsAdhoc": "8", **HAIKU_OVERRIDES},
        task_type="run",
        expected_files=["adhoc_result.json", "reports/report.md", "reports/report.html"],
        timeout=1200,
    ),
    TestCase(
        name="prs",
        job_type="ai-adhoc",
        params={
            "Prompt": "open prs",
            "Skill": "ygs-pr-queue",
            # JiraBoards: skip full board scan; go straight to the standup board.
            # Same value as standup tests — ygs-pr-queue queries active sprint for linked open PRs.
            "JiraBoards": os.environ.get("JIRA_BOARDS", ""),
            **HAIKU_OVERRIDES,
        },
        task_type="run",
        expected_files=["adhoc_result.json"],
        timeout=900,
        # ygs-pr-queue uses disable-model-invocation: true — no Claude call, so SKILLS_INVOKED is not emitted.
        required_context_keys=["SKILL", "SKILL_LOADED", "YGS_SKILLS_COUNT",
                                "YGS_SKILLS_INSTALLED", "YGS_SKILLS_REPO_COMMIT"],
    ),
    TestCase(
        name="pr-comments",
        job_type="ai-adhoc",
        params={"Prompt": f"pr comments {PR_URL}", "Skill": "ygs-pr-comments", "MaxTurnsAdhoc": "20", **HAIKU_OVERRIDES},
        task_type="run",
        expected_files=["adhoc_result.json", "reports/report.md", "reports/report.html"],
        timeout=600,
        requires=["PR_URL"],
    ),
    # ask: general Q&A via ygs-ask — no Jira/GitHub data needed, pure reasoning.
    # PREREQUISITES: commit ygs-ask to you-got-skills, rebuild ai-dev-tools Docker
    # image (make docker-build), deploy ai-adhoc.yaml (deploy-ai-jira-workflows.sh).
    TestCase(
        name="ask",
        job_type="ai-adhoc",
        params={
            "Skill": "ygs-ask",
            "Prompt": "What is a Kubernetes pod and how does it differ from a container?",
            "MaxTurnsAdhoc": "5",
            "MaxClaudeProcessTimeout": "270",
            **HAIKU_OVERRIDES,
        },
        task_type="run",
        expected_files=["adhoc_result.json", "reports/report.md"],
        timeout=600,
    ),
    # ask-jira: general Q&A that may require live Jira data.
    # PREREQUISITES: same as ask + Jira credentials in ai-dev-credentials secret.
    TestCase(
        name="ask-jira",
        job_type="ai-adhoc",
        params={
            "Skill": "ygs-ask",
            "Prompt": "How many open issues are in the current sprint?",
            "MaxTurnsAdhoc": "20",
            **HAIKU_OVERRIDES,
        },
        task_type="run",
        expected_files=["adhoc_result.json"],
        timeout=600,
    ),
    # extra-skills: verify EXTRA_SKILLS_REPOS installs multiple public skill repos.
    # Uses comma-separated plain format — safe for YAML template substitution
    # (JSON with double quotes / curly braces breaks YAML template expansion).
    # Prefix "skills-cli:" forces npx skills add path for that entry.
    # JSON format works fine when set via org config (K8s secret injection path).
    TestCase(
        name="extra-skills",
        job_type="ai-adhoc",
        params={
            "Skill": "ygs-ask",
            "Prompt": "What is a Kubernetes pod?",
            "MaxTurnsAdhoc": "5",
            "MaxClaudeProcessTimeout": "270",
            # you-got-skills via sparse git clone + nutlope/hallmark via skills-cli
            "ExtraSkillsRepos": "https://github.com/bhatti/you-got-skills.git,skills-cli:nutlope/hallmark",
            **HAIKU_OVERRIDES,
        },
        task_type="run",
        expected_files=["adhoc_result.json", "reports/report.md"],
        timeout=300,
        required_context_keys=["YGS_SKILLS_COUNT", "YGS_SKILLS_INSTALLED",
                                "YGS_SKILLS_REPO_COMMIT", "EXTRA_SKILLS_YOU_GOT_SKILLS_COUNT",
                                "SKILLS_INVOKED"],
    ),
    TestCase(
        name="standup",
        job_type="ai-standup-jira",
        params={**HAIKU_OVERRIDES},
        task_type="synthesize",
        expected_files=["standup_brief.md", "synthesize_result.json", "reports/report.md", "reports/report.html"],
        timeout=900,
        cron=True,
        required_context_keys=["SELECTED_MODEL", "SELECTED_TRACKER", "ISSUE_COUNT",
                                "SKILL", "SKILL_LOADED", "YGS_SKILLS_COUNT", "SKILLS_INVOKED"],
    ),
    TestCase(
        name="standup-post",
        job_type="ai-standup-jira",
        params={**HAIKU_OVERRIDES},
        task_type="post",
        expected_files=["reports/report.md", "reports/report.html", "reports/post_result.json", "reports/slack_message.txt"],
        timeout=900,
        cron=True,
    ),
    TestCase(
        name="review-post",
        job_type="ai-jira-review",
        params={"PRUrl": PR_URL, "MaxTurnsReview": "8", **HAIKU_OVERRIDES},
        task_type="post",
        expected_files=["reports/report.md", "reports/report.html", "reports/findings.json", "reports/post_result.json", "reports/slack_message.txt"],
        timeout=1200,
        requires=["PR_URL"],
    ),
]


# ── Formicary API helpers ────────────────────────────────────────────────────

def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def _test_description(job_type: str, params: dict) -> str:
    """Derive a short dashboard description for a test job."""
    # Show the most useful param: Prompt, Query, PRUrl, IssueNumber, Skill
    for key in ("Prompt", "Query", "PRUrl", "IssueNumber", "Skill"):
        val = params.get(key, "")
        if val and not val.startswith("us.anthropic"):
            suffix = val.rstrip("/").rsplit("/", 1)[-1] if val.startswith("http") else val
            return f"test: {suffix}"[:80]
    return f"test: {job_type}"[:80]


def submit_job(job_type: str, params: dict) -> str:
    """Submit a job, return its ID."""
    payload = {
        "job_type": job_type,
        "params": params,
        "description": _test_description(job_type, params),
    }
    resp = requests.post(
        f"{FORMICARY_URL}/api/v1/jobs/requests",
        headers=_headers(),
        json=payload,
        verify=TLS_VERIFY,
        timeout=20,
    )
    if not resp.ok:
        raise RuntimeError(f"submit failed HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    job = data.get("job_request") or data
    return job["id"]


def trigger_cron_job(job_type: str, params: dict) -> str:
    """Trigger a pending cron job to run now; return the job ID.

    Cron jobs (like standup) have a PENDING scheduled request — we inject the
    test params and trigger it immediately via /api/v1/jobs/requests/:id/trigger.
    If no PENDING exists, falls back to a fresh submit.
    """
    # Find a WAITING/PENDING request for this cron job
    resp = requests.get(
        f"{FORMICARY_URL}/api/jobs/requests",
        headers=_headers(),
        params={"job_type": job_type, "job_state": "WAITING"},
        verify=TLS_VERIFY,
        timeout=15,
    )
    job_id = None
    if resp.ok:
        records = resp.json().get("Records") or resp.json().get("records") or []
        if records:
            job_id = records[0].get("id")

    if not job_id:
        # No PENDING cron slot — submit a fresh request
        return submit_job(job_type, params)

    # Trigger the pending request to run immediately
    trigger_resp = requests.post(
        f"{FORMICARY_URL}/api/v1/jobs/requests/{job_id}/trigger",
        headers=_headers(),
        verify=TLS_VERIFY,
        timeout=10,
    )
    if not trigger_resp.ok:
        raise RuntimeError(f"trigger failed HTTP {trigger_resp.status_code}: {trigger_resp.text[:300]}")
    return job_id


def poll_until_done(job_id: str, timeout: int) -> dict:
    """Poll job status until terminal state or timeout. Returns the job dict."""
    deadline = time.time() + timeout
    terminal = {"COMPLETED", "FAILED", "CANCELLED", "FATAL_ERROR"}
    while time.time() < deadline:
        resp = requests.get(
            f"{FORMICARY_URL}/api/jobs/requests/{job_id}",
            headers=_headers(),
            verify=TLS_VERIFY,
            timeout=15,
        )
        if not resp.ok:
            raise RuntimeError(f"poll failed HTTP {resp.status_code}")
        job = resp.json()
        state = job.get("job_state", "")
        if state in terminal:
            return job
        elapsed = int(time.time() - (deadline - timeout))
        print(f"  [{job_id[:12]}] state={state} elapsed={elapsed}s ...", flush=True)
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"job {job_id} did not complete within {timeout}s")


def list_artifacts(job_id: str) -> list[dict]:
    resp = requests.get(
        f"{FORMICARY_URL}/api/artifacts",
        headers=_headers(),
        params={"job_request_id": job_id},
        verify=TLS_VERIFY,
        timeout=15,
    )
    if not resp.ok:
        return []
    return resp.json().get("Records", [])


def download_zip(sha256: str) -> bytes:
    resp = requests.get(
        f"{FORMICARY_URL}/api/artifacts/{sha256}/download",
        headers=_headers(),
        verify=TLS_VERIFY,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def get_task_zip(job_id: str, task_type: str) -> tuple[str, bytes] | tuple[None, None]:
    """Return (sha, bytes) of the task's ZIP artifact, or (None, None) if not found."""
    for art in list_artifacts(job_id):
        if art.get("task_type") == task_type and art.get("name", "").endswith(".zip"):
            return art["sha256"], download_zip(art["sha256"])
    return None, None


def get_console_log(job_id: str, task_type: str) -> str:
    """Download and return the console log for a task, for error diagnostics."""
    for art in list_artifacts(job_id):
        if art.get("task_type") == task_type and "console" in art.get("name", ""):
            try:
                return download_zip(art["sha256"]).decode("utf-8", errors="replace")
            except Exception:
                pass
    return ""


def get_task_contexts(job: dict, task_type: str) -> dict[str, str]:
    """Return task context variables for a given task type from a completed job.

    Fetches the job execution record and flattens contexts from the matching task
    into a plain {name: value} dict.  Returns {} if execution data is unavailable.
    """
    exec_id = job.get("job_execution_id", "")
    if not exec_id:
        return {}
    try:
        resp = requests.get(
            f"{FORMICARY_URL}/api/v1/jobs/executions/{exec_id}",
            headers=_headers(),
            verify=TLS_VERIFY,
            timeout=15,
        )
        if not resp.ok:
            return {}
        je = resp.json().get("job_execution") or resp.json()
        for task in je.get("tasks") or []:
            if task.get("task_type") == task_type:
                return {c["name"]: c.get("value", "") for c in task.get("contexts") or []}
    except Exception:
        pass
    return {}


def validate_task_context(job: dict, task_type: str, required_keys: list[str]) -> str | None:
    """Return an error string if any required context key is missing/empty, else None."""
    ctx = get_task_contexts(job, task_type)
    missing = [k for k in required_keys if not ctx.get(k)]
    if missing:
        present = {k: ctx[k] for k in ctx if k in required_keys}
        return f"task context missing keys {missing}; present={present}"
    return None


# ── Test runner ──────────────────────────────────────────────────────────────

@dataclass
class TestResult:
    name: str
    job_id: str = ""
    state: str = "NOT_RUN"
    passed: bool = False
    errors: list[str] = field(default_factory=list)
    zip_files: list[str] = field(default_factory=list)
    output_snippet: str = ""
    elapsed: float = 0.0


def run_test(tc: TestCase) -> TestResult:
    r = TestResult(name=tc.name)
    t0 = time.time()

    # Check required env vars — skip instead of failing so --tests all stays clean.
    missing = [v for v in tc.requires if not os.environ.get(v)]
    if missing:
        r.state = "SKIPPED"
        r.passed = True  # skipped != failed; doesn't count against pass rate
        r.errors = [f"SKIP: {', '.join(missing)} not set — add to ~/.zshrc to enable this test"]
        r.elapsed = 0.0
        print(f"\n⏭  [{tc.name}] SKIPPED — {r.errors[0]}", flush=True)
        return r

    try:
        action = "triggering cron" if tc.cron else "submitting"
        print(f"\n▶ [{tc.name}] {action} {tc.job_type} ...", flush=True)
        if tc.cron:
            r.job_id = trigger_cron_job(tc.job_type, tc.params)
        else:
            r.job_id = submit_job(tc.job_type, tc.params)
        print(f"  [{tc.name}] job_id={r.job_id}  {FORMICARY_URL}/dashboard/jobs/requests/{r.job_id}?", flush=True)

        job = poll_until_done(r.job_id, tc.timeout)
        r.state = job.get("job_state", "UNKNOWN")
        r.elapsed = time.time() - t0

        if r.state != "COMPLETED":
            r.errors.append(f"job ended with state={r.state}")
            # Dump console log for debugging
            console = get_console_log(r.job_id, tc.task_type)
            if console:
                tail = console[-2000:]
                print(f"  [{tc.name}] CONSOLE TAIL:\n{tail}", flush=True)
            return r

        # Download and inspect ZIP
        sha, zdata = get_task_zip(r.job_id, tc.task_type)
        if zdata is None:
            r.errors.append(f"no ZIP artifact found for task_type={tc.task_type}")
            return r

        with zipfile.ZipFile(io.BytesIO(zdata)) as zf:
            r.zip_files = zf.namelist()
            for expected in tc.expected_files:
                if not any(f == expected or f.endswith("/" + expected) for f in r.zip_files):
                    r.errors.append(f"MISSING: {expected}")

            # Extract a snippet from result/report files for display
            snippet_candidates = [
                "reports/result.json", "reports/report.md",
                "adhoc_result.json", "review_result.json",
                "synthesize_result.json", "standup_brief.md",
            ]
            for candidate in snippet_candidates:
                try:
                    content = zf.read(candidate).decode("utf-8", errors="replace")
                    r.output_snippet = content[:600]
                    break
                except KeyError:
                    pass

        if tc.required_context_keys:
            ctx_err = validate_task_context(job, tc.task_type, tc.required_context_keys)
            if ctx_err:
                r.errors.append(ctx_err)

        r.passed = len(r.errors) == 0

    except Exception as e:
        r.errors.append(str(e))
        r.elapsed = time.time() - t0

    return r


def print_result(r: TestResult) -> None:
    if r.state == "SKIPPED":
        return  # already printed inline in run_test
    status = "✅ PASS" if r.passed else "❌ FAIL"
    print(f"\n{'=' * 60}")
    print(f"{status}  {r.name}  [{r.elapsed:.1f}s]  state={r.state}")
    if r.job_id:
        print(f"  job: {FORMICARY_URL}/dashboard/jobs/requests/{r.job_id}?")
    if r.zip_files:
        print(f"  artifacts ({len(r.zip_files)}): {', '.join(r.zip_files[:12])}")
    if r.errors:
        for e in r.errors:
            print(f"  ERROR: {e}")
    if r.output_snippet:
        print(f"  --- output snippet ---")
        for line in r.output_snippet.splitlines()[:20]:
            print(f"  {line}")
    print(f"{'=' * 60}", flush=True)


# ── Infrastructure health check ──────────────────────────────────────────────

def _check_infra_health() -> None:
    """Verify Docker daemon and ant k8s cluster (kind-hedge) are reachable.

    Fails fast with a clear error rather than submitting jobs that queue forever
    on an unavailable cluster.  Uses subprocess so no Python k8s client needed.
    """
    import subprocess as _sp

    # 1. Docker daemon
    r = _sp.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        print(f"\nERROR: Docker daemon is not running or not accessible.\n"
              f"  docker info failed: {r.stderr.strip()[:200]}\n"
              f"  Start Docker Desktop (or 'sudo systemctl start docker') and retry.",
              file=sys.stderr)
        sys.exit(1)
    docker_ver = r.stdout.strip()

    # 2. Ant worker k8s cluster — look for a kind context first, then fall back to current context.
    kube_ctx = os.environ.get("KUBE_CONTEXT", "")
    if not kube_ctx:
        # Prefer kind-hedge; fall back to whatever kubectl is currently using.
        ctx_result = _sp.run(["kubectl", "config", "get-contexts", "-o", "name"],
                             capture_output=True, text=True, timeout=10)
        contexts = ctx_result.stdout.splitlines()
        kind_ctxs = [c for c in contexts if c.startswith("kind-")]
        kube_ctx = kind_ctxs[0] if kind_ctxs else ""

    ctx_args = ["--context", kube_ctx] if kube_ctx else []
    k8s = _sp.run(["kubectl", *ctx_args, "get", "nodes", "--no-headers"],
                  capture_output=True, text=True, timeout=20)
    if k8s.returncode != 0:
        ctx_label = kube_ctx or "(default context)"
        print(f"\nERROR: Kubernetes cluster {ctx_label} is not reachable.\n"
              f"  kubectl get nodes failed: {k8s.stderr.strip()[:300]}\n"
              f"  Check that Docker is running and kind cluster is healthy:\n"
              f"    docker ps | grep kind\n"
              f"    kubectl --context {ctx_label} get nodes",
              file=sys.stderr)
        sys.exit(1)

    ready = sum(1 for ln in k8s.stdout.splitlines() if "Ready" in ln)
    ctx_label = kube_ctx or "default"
    print(f"✓ Docker {docker_ver}  |  k8s cluster '{ctx_label}' — {ready} node(s) Ready")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tests", default="",
                        help="Comma-separated test names to run (default: jira-query,analyze)")
    parser.add_argument("--parallel", type=int, default=1,
                        help=(
                            "Max concurrent jobs (default: 1). "
                            "POLICY: Run tests one at a time to avoid overloading the ant worker "
                            "and to produce deterministic, easy-to-diagnose results. "
                            "Values > 1 are allowed but not recommended."
                        ))
    parser.add_argument("--timeout", type=int, default=0,
                        help="Per-test timeout override in seconds (0=use test default)")
    parser.add_argument("--list", action="store_true",
                        help="List available test names and exit")
    parser.add_argument("--skip-health", action="store_true",
                        help="Skip Docker/k8s infra health check (use when Docker Desktop is slow)")
    args = parser.parse_args()

    if args.list:
        print("Available tests:")
        for tc in ALL_TESTS:
            print(f"  {tc.id():<20} job_type={tc.job_type}")
        return

    # Validate env — nothing is hardcoded, all must come from environment
    errors = []
    if not TOKEN:
        errors.append("FORMICARY_TOKEN not set")
    if not FORMICARY_URL or FORMICARY_URL == "https://.nip.io":
        errors.append("EC2_IP (or FORMICARY_URL) not set")
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        print("Run: source ~/.zshrc  (or export EC2_IP=<host> FORMICARY_TOKEN=<token> PR_URL=<url>)",
              file=sys.stderr)
        sys.exit(1)

    # Select tests — "all" runs every test case
    if args.tests and args.tests.strip().lower() == "all":
        selected = list(ALL_TESTS)
    elif args.tests:
        names = {n.strip() for n in args.tests.split(",")}
        selected = [tc for tc in ALL_TESTS if tc.id() in names]
        if not selected:
            print(f"ERROR: no matching tests for: {args.tests}", file=sys.stderr)
            print(f"Available: {', '.join(tc.id() for tc in ALL_TESTS)}")
            sys.exit(1)
    else:
        # Default: first 2 tests
        selected = ALL_TESTS[:2]

    if args.timeout:
        for tc in selected:
            tc.timeout = args.timeout

    # Re-read PR_URL after _load_zshrc() ran (module-level read happened before sourcing).
    pr_url_live = os.environ.get("PR_URL", "")
    for tc in selected:
        if not pr_url_live:
            break
        if tc.id() in ("review", "review-post"):
            tc.params = {**tc.params, "PRUrl": pr_url_live}
        elif tc.id() == "pr-comments":
            tc.params = {**tc.params, "Prompt": f"pr comments {pr_url_live}"}

    print(f"\nFormicary: {FORMICARY_URL}")
    print(f"Running {len(selected)} test(s) with parallelism={args.parallel}: "
          f"{', '.join(tc.id() for tc in selected)}")
    print(f"Model override: {HAIKU}")
    if pr_url_live:
        print(f"PR_URL: {pr_url_live}")
    print()

    # Pre-flight: verify Docker + ant k8s cluster are healthy before submitting jobs.
    # Jobs run on Kubernetes pods on the ant worker cluster; if it's unavailable
    # they'll queue forever and waste the test timeout budget.
    if not args.skip_health:
        _check_infra_health()

    # POLICY: default parallelism is 1 — run tests sequentially to avoid
    # overloading the single ant worker and to produce deterministic results.
    # Each job submission starts a Kubernetes pod on the ant cluster; concurrent
    # submissions can exhaust pod slots, cause queue stalls, and interleave logs.
    results: list[TestResult] = []
    if args.parallel <= 1:
        # Sequential path — avoids ThreadPoolExecutor overhead and race conditions.
        for tc in selected:
            r = run_test(tc)
            results.append(r)
            print_result(r)
    else:
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = {pool.submit(run_test, tc): tc for tc in selected}
            for fut in as_completed(futures):
                r = fut.result()
                results.append(r)
                print_result(r)

    # Summary — skipped tests count as passed (not failures)
    skipped = [r for r in results if r.state == "SKIPPED"]
    ran = [r for r in results if r.state != "SKIPPED"]
    passed = sum(1 for r in ran if r.passed)
    print(f"\n{'=' * 60}")
    print(f"SUMMARY: {passed}/{len(ran)} passed" + (f"  ({len(skipped)} skipped)" if skipped else ""))
    for r in sorted(results, key=lambda x: x.name):
        if r.state == "SKIPPED":
            print(f"  ⏭  {r.name} (skipped — {r.errors[0].replace('SKIP: ', '') if r.errors else 'missing env vars'})")
        else:
            status = "✅" if r.passed else "❌"
            errors = f"  → {'; '.join(r.errors)}" if r.errors else ""
            print(f"  {status} {r.name} ({r.elapsed:.1f}s){errors}")
    print(f"{'=' * 60}")

    sys.exit(0 if passed == len(ran) else 1)


if __name__ == "__main__":
    main()
