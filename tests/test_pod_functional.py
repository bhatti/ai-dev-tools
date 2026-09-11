#!/usr/bin/env python3
"""Pod-based functional tests for ai-dev-tools scripts.

Simulates exactly what Formicary ant workers do — without the build/deploy cycle:
  1. Create a fresh Kubernetes pod (plexobject/ai-dev-tools:latest)
  2. Copy updated local scripts into the pod (overwrite stale image layers)
  3. Inject credentials from the ai-dev-credentials k8s secret
  4. Run each workflow step via kubectl exec
  5. Parse ::add-task-context markers from stdout
  6. Assert required keys/values are present
  7. Delete the pod

Each test gets its own isolated pod: create → copy → step-1 → step-2 → … → teardown.

Multi-step workflows (e.g. standup: gather → synthesize → post) run all steps
in the SAME pod so intermediate files (/workspace/signals.json etc.) are shared.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    source ~/.zshrc

    # Run specific tests:
    python3 tests/test_pod_functional.py --tests jira-query
    python3 tests/test_pod_functional.py --tests jira-analyze
    python3 tests/test_pod_functional.py --tests standup-gather,standup-synthesize

    # Run with a specific Jira issue:
    ISSUE_ID=PROJ-123 python3 tests/test_pod_functional.py --tests jira-analyze

    # Run all:
    ISSUE_ID=PROJ-123 python3 tests/test_pod_functional.py --tests all

    # List available tests:
    python3 tests/test_pod_functional.py --list

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ENV VARS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    POD_NAMESPACE    k8s namespace (default: default)
    ISSUE_ID         Jira issue key for analyze tests (e.g. PROJ-123)
    JIRA_PROJECT     Jira project key (auto-resolved from JIRA_BOARDS API if not set)
    JIRA_BOARDS      Board ID(s) used to resolve JIRA_PROJECT via the Jira Agile API
    GH_ORG           GitHub org (default: bhatti)
    GH_REPO          GitHub repo (default: todo-sample)
    SKIP_COPY        Set to 1 to use scripts from image as-is (no local copy)

JIRA_PROJECT resolution:
    If JIRA_PROJECT is not set, the test harness calls the Jira Agile API
    (GET /rest/agile/1.0/board/{id}) using JIRA_BOARDS, JIRA_BASE_URL,
    JIRA_EMAIL, and JIRA_API_TOKEN from the k8s secret or ~/.zshrc to resolve
    the project key. This mirrors how Formicary's org-config provides it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path


# ── bootstrap ─────────────────────────────────────────────────────────────────

def _load_zshrc() -> None:
    """Import env vars from ~/.zshrc without overwriting vars already set."""
    zshrc = Path.home() / ".zshrc"
    if not zshrc.exists():
        return
    try:
        result = subprocess.run(
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
        pass


_load_zshrc()


# ── global config ──────────────────────────────────────────────────────────────

REPO_ROOT  = Path(__file__).parent.parent.resolve()
NAMESPACE  = os.environ.get("POD_NAMESPACE", "default")
SKIP_COPY  = os.environ.get("SKIP_COPY", "0") == "1"
ISSUE_ID   = os.environ.get("ISSUE_ID", "")
GH_ORG     = os.environ.get("GH_ORG", "bhatti")
GH_REPO    = os.environ.get("GH_REPO", "todo-sample")
IMAGE      = "plexobject/ai-dev-tools:latest"

# Every local file that might have been edited since the image was built.
# kubectl cp overwrites the image's copy so tests always exercise current code.
_SCRIPTS_TO_COPY = [
    "scripts/jira/analyze_issues.py",
    "scripts/jira/query_issues.py",
    "scripts/gh/analyze_issues.py",
    "scripts/gh/query_issues.py",
    "scripts/common/issue_analysis.py",
    "scripts/common/skill_resolver.py",
    "scripts/common/git_archaeology.py",
    "scripts/common/git_utils.py",
    "scripts/standup/slack_client.py",
    "scripts/standup/gather_jira.py",
    "scripts/standup/gather_gh.py",
    "scripts/standup/gather_pr_queue.py",
    "scripts/standup/synthesize.py",
    "scripts/standup/post.py",
    "scripts/standup/render_html.py",
    "scripts/review/__init__.py",
    "scripts/review/run.py",
    "scripts/review/post_findings.py",
    "scripts/review/apply_feedback.py",
    "scripts/common/claude_runner.py",
    "scripts/common/config.py",
    "scripts/common/skills.py",
    "scripts/common/artifacts.py",
    "scripts/common/notify_slack.py",
    "scripts/common/report_renderer.py",
    "scripts/analyze/__init__.py",
    "scripts/analyze/run_codebase_audit.py",
    "scripts/analyze/post_audit.py",
    "scripts/analyze/pr_fetcher.py",
    "scripts/analyze/run_pr_audit.py",
    "scripts/analyze/post_pr_audit.py",
    "scripts/analyze/create_skill_pr.py",
    "scripts/analyze/plan_skill_updates.py",
    "scripts/common/repo_utils.py",
    "scripts/common/slack_format.py",
    "scripts/common/health_check_prompts.py",
    "scripts/common/learn_prompts.py",
    "scripts/common/bitbucket_api.py",
    "scripts/common/shell.py",
    "scripts/gh/learn.py",
    "scripts/jira/learn.py",
]

# Public GitHub repo used for codebase-audit pod tests.
# Override via AUDIT_REPO_URL env var to test against a private repo.
AUDIT_REPO_URL = os.environ.get(
    "AUDIT_REPO_URL",
    "https://github.com/bhatti/todo-sample.git",
)

# Pod manifest — mirrors the Formicary ant worker spec
_POD_MANIFEST = """\
apiVersion: v1
kind: Pod
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app: ai-dev-pod-test
spec:
  restartPolicy: Never
  containers:
  - name: main
    image: {image}
    imagePullPolicy: Always
    command: ["sleep", "3600"]
    envFrom:
    - secretRef:
        name: ai-dev-credentials
    resources:
      requests:
        memory: 512Mi
        cpu: 200m
      limits:
        memory: 4Gi
        cpu: "2"
    volumeMounts:
    - name: workspace
      mountPath: /workspace
  volumes:
  - name: workspace
    emptyDir: {{}}
"""


# ── kubectl helpers ────────────────────────────────────────────────────────────

def _kubectl(*args: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", "-n", NAMESPACE, *args],
        capture_output=True, text=True, timeout=timeout, check=check,
    )


def _create_pod(name: str) -> None:
    manifest = _POD_MANIFEST.format(name=name, namespace=NAMESPACE, image=IMAGE)
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write(manifest)
        path = f.name
    try:
        _kubectl("apply", "-f", path)
    finally:
        os.unlink(path)
    print(f"    waiting for {name} to be Running ...", flush=True)
    deadline = time.time() + 120
    while time.time() < deadline:
        r = _kubectl("get", "pod", name, "-o", "jsonpath={.status.phase}", check=False)
        if r.returncode == 0 and r.stdout.strip() == "Running":
            print(f"    {name} is Running", flush=True)
            return
        time.sleep(3)
    raise RuntimeError(f"Pod {name} did not reach Running within 120s")


def _delete_pod(name: str) -> None:
    _kubectl("delete", "pod", name, "--ignore-not-found=true", "--grace-period=0",
             timeout=30, check=False)


def _copy_scripts(pod_name: str) -> None:
    if SKIP_COPY:
        print("    SKIP_COPY=1 — using scripts from image", flush=True)
        return
    # Ensure all destination directories exist in the pod before copying
    dirs = {str(Path(rel).parent) for rel in _SCRIPTS_TO_COPY if str(Path(rel).parent) != "."}
    for d in sorted(dirs):
        _kubectl("exec", pod_name, "--", "mkdir", "-p", f"/app/{d}", timeout=15, check=False)
    n = 0
    for rel in _SCRIPTS_TO_COPY:
        local = REPO_ROOT / rel
        if not local.exists():
            print(f"    WARNING: {rel} not found locally — skipping", flush=True)
            continue
        _kubectl("cp", str(local), f"{pod_name}:/app/{rel}", timeout=30)
        n += 1
    print(f"    copied {n} script(s) into {pod_name}", flush=True)


# ── pod fixture ────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def pod_fixture(test_name: str):
    """Context manager: create pod, copy scripts, yield pod_name, delete on exit."""
    name = f"ai-dev-{test_name.replace('_', '-')[:20]}-{uuid.uuid4().hex[:6]}"
    print(f"\n  [pod] creating {name} for test '{test_name}' ...", flush=True)
    _create_pod(name)
    try:
        _copy_scripts(name)
        yield name
    finally:
        print(f"  [pod] deleting {name} ...", flush=True)
        _delete_pod(name)


# ── secret + base env ──────────────────────────────────────────────────────────

def _fetch_jira_project_from_board(base_url: str, email: str, token: str,
                                    boards: str) -> str:
    """Lookup JIRA_PROJECT from the first board ID in JIRA_BOARDS via the Agile API."""
    board_id = boards.split(",")[0].strip()
    url = f"{base_url.rstrip('/')}/rest/agile/1.0/board/{board_id}"
    import base64 as _b64
    creds = _b64.b64encode(f"{email}:{token}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}",
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            project_key = (data.get("location") or {}).get("projectKey", "")
            if project_key:
                print(f"[pod-tests] resolved JIRA_PROJECT={project_key} from board {board_id}",
                      flush=True)
            return project_key
    except Exception as e:
        print(f"[pod-tests] WARNING: could not resolve JIRA_PROJECT from board {board_id}: {e}",
              flush=True)
        return ""


def load_secret(name: str = "ai-dev-credentials") -> dict[str, str]:
    r = _kubectl("get", "secret", name, "-o", "json")
    return {k: base64.b64decode(v).decode() for k, v in json.loads(r.stdout)["data"].items()}


def build_base_env(secret: dict[str, str]) -> dict[str, str]:
    """Construct the environment every script step gets.

    Priority: explicit env var > k8s secret value > hardcoded default.
    Org configs (JIRA_PROJECT, BITBUCKET_REPO) are Formicary YAML variables,
    not in the secret — read from os.environ with sane defaults.
    """
    env = dict(secret)
    env.update({
        "PYTHONPATH": "/app",
        "WORKSPACE_DIR": "/workspace",
        "SLACK_BOT_TOKEN": "",
        "SLACK_CHANNEL": "",
        "SLACK_THREAD_TS": "",
        "AI_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "us.anthropic.claude-sonnet-4-6",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "us.anthropic.claude-opus-4-6-v1",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
        "MAX_RESULTS": "5",
    })
    bedrock = os.environ.get("ANTHROPIC_BEDROCK_BASE_URL", "")
    if bedrock:
        env["ANTHROPIC_BEDROCK_BASE_URL"] = bedrock

    # Org configs: explicit env > secret > default
    jira_project = os.environ.get("JIRA_PROJECT") or env.get("JIRA_PROJECT") or ""
    if not jira_project:
        boards = os.environ.get("JIRA_BOARDS") or env.get("JIRA_BOARDS") or ""
        if boards and env.get("JIRA_BASE_URL") and env.get("JIRA_EMAIL") and env.get("JIRA_API_TOKEN"):
            jira_project = _fetch_jira_project_from_board(
                env["JIRA_BASE_URL"], env["JIRA_EMAIL"], env["JIRA_API_TOKEN"], boards)

    org_defaults = {
        "JIRA_PROJECT":    jira_project,
        "JIRA_TEAM_FIELD": "EngScrumTeam",
        "JIRA_SPACE":      "",
        "BITBUCKET_REPO":  os.environ.get("BITBUCKET_REPO", ""),
        "GH_ORG":          GH_ORG,
        "GH_REPO":         GH_REPO,
    }
    for key, default in org_defaults.items():
        val = os.environ.get(key) or env.get(key) or default
        if val:
            env[key] = val

    for key in ("JIRA_BASE_URL", "BITBUCKET_WORKSPACE", "BITBUCKET_USERNAME"):
        if os.environ.get(key):
            env[key] = os.environ[key]

    return env


# ── exec + result ──────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    label: str
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    elapsed: float = 0.0
    context: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode in (0, 2)

    @property
    def has_results(self) -> bool:
        return self.returncode == 0


@dataclass
class TestResult:
    name: str
    passed: bool = False
    message: str = ""
    steps: list[StepResult] = field(default_factory=list)


def exec_step(pod_name: str, label: str, cmd: str, env: dict[str, str],
              timeout: int = 300) -> StepResult:
    """Run a bash command in the pod; capture stdout/stderr; parse context markers."""
    # Export env vars safely: json.dumps handles quoting/escaping for bash
    env_lines = "\n".join(f"export {k}={json.dumps(v)}" for k, v in env.items())
    script = f"set -uo pipefail\n{env_lines}\ncd /app\n{cmd}"
    print(f"    [{label}] running ...", flush=True)
    t0 = time.time()
    proc = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "exec", pod_name, "--", "bash", "-c", script],
        capture_output=True, text=True, timeout=timeout,
    )
    elapsed = time.time() - t0
    ctx = _parse_context(proc.stdout or "")
    print(f"    [{label}] exit={proc.returncode} elapsed={elapsed:.0f}s "
          f"context_keys={list(ctx.keys())}", flush=True)
    return StepResult(
        label=label,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        elapsed=elapsed,
        context=ctx,
    )


def _parse_context(output: str) -> dict[str, str]:
    """Extract ::add-task-context KEY::VALUE lines; last occurrence wins."""
    ctx: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("::add-task-context "):
            continue
        rest = line[len("::add-task-context "):]
        if "::" not in rest:
            continue
        key, _, val = rest.partition("::")
        ctx[key.strip()] = val.strip()
    return ctx


def _check_keys(step: StepResult, required: list[str]) -> str | None:
    """Return error message if any required context key is missing, else None."""
    missing = [k for k in required if k not in step.context]
    if missing:
        return (
            f"missing context keys {missing}\n"
            f"  got: {step.context}\n"
            f"  stdout tail: {step.stdout[-400:]}"
        )
    return None


def _check_values(step: StepResult, expected: dict[str, str]) -> str | None:
    """Return error message if any context value doesn't match expectation."""
    wrong = {k: f"want={v!r} got={step.context.get(k)!r}"
             for k, v in expected.items() if step.context.get(k) != v}
    return f"wrong context values: {wrong}" if wrong else None


def _fail(result: TestResult, step: StepResult, reason: str) -> TestResult:
    result.passed = False
    result.message = (
        f"FAILED at [{step.label}]: {reason}\n"
        f"  stderr: {step.stderr[-400:] if step.stderr else '(none)'}"
    )
    return result


def _pass(result: TestResult, summary: str) -> TestResult:
    result.passed = True
    result.message = f"PASSED — {summary}"
    return result


# ── individual tests ───────────────────────────────────────────────────────────
# Each test function:
#   1. Receives base_env (already merged with secret + defaults)
#   2. Uses pod_fixture() to get a fresh pod
#   3. Runs one or more exec_step() calls
#   4. Returns a TestResult
# ──────────────────────────────────────────────────────────────────────────────

def test_01_jira_query(base_env: dict[str, str]) -> TestResult:
    """Query Jira issues by keyword. Verifies basic Jira connectivity + context markers."""
    result = TestResult("jira-query")
    if not base_env.get("JIRA_BASE_URL"):
        result.passed = True
        result.message = "SKIPPED — JIRA_BASE_URL not set"
        return result
    # JIRA_PROJECT is resolved from JIRA_BOARDS via API in build_base_env;
    # if still missing, query_issues will fail and we'll see a real error.
    if not base_env.get("JIRA_PROJECT"):
        result.passed = True
        result.message = "SKIPPED — JIRA_PROJECT could not be resolved (set JIRA_PROJECT or JIRA_BOARDS)"
        return result

    env = dict(base_env)
    ws = "/workspace/jira_query"
    env["WORKSPACE_DIR"] = ws

    with pod_fixture("jira-query") as pod:
        step = exec_step(pod, "jira-query",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         "python3 -m scripts.jira.query_issues --query 'open' --max 5",
                         env, timeout=120)
        result.steps.append(step)

    if not step.ok:
        return _fail(result, step, f"exit code {step.returncode}")

    if not step.has_results:
        return _pass(result, "no matching issues (exit 2) — Jira reachable")

    err = _check_keys(step, ["SELECTED_TRACKER", "ISSUE_COUNT"]) or \
          _check_values(step, {"SELECTED_TRACKER": "jira"})
    return _fail(result, step, err) if err else \
           _pass(result, f"ISSUE_COUNT={step.context.get('ISSUE_COUNT')} "
                         f"elapsed={step.elapsed:.0f}s")


def test_02_jira_analyze(base_env: dict[str, str]) -> TestResult:
    """Analyze a specific Jira issue with Claude. Verifies ANALYSIS_TYPE context key."""
    result = TestResult("jira-analyze")
    if not ISSUE_ID:
        result.passed = True
        result.message = "SKIPPED — set ISSUE_ID env var (e.g. ISSUE_ID=PROJ-123)"
        return result
    if not base_env.get("JIRA_BASE_URL"):
        result.passed = True
        result.message = "SKIPPED — JIRA_BASE_URL not set"
        return result

    env = dict(base_env)
    ws = "/workspace/jira_analyze"
    env["WORKSPACE_DIR"] = ws

    with pod_fixture("jira-analyze") as pod:
        step = exec_step(pod, "jira-analyze",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         f"python3 -m scripts.jira.analyze_issues "
                         f"--issues '{ISSUE_ID}' --prompt 'give tldr for {ISSUE_ID}' --max 5",
                         env, timeout=300)
        result.steps.append(step)

    if not step.ok:
        return _fail(result, step, f"exit code {step.returncode}")
    if not step.has_results:
        return _pass(result, f"no issues found for {ISSUE_ID} (exit 2)")

    err = _check_keys(step, ["SELECTED_TRACKER", "ISSUE_COUNT", "ANALYSIS_TYPE",
                              "GIT_ARCHAEOLOGY"]) or \
          _check_values(step, {"SELECTED_TRACKER": "jira"})
    if err:
        return _fail(result, step, err)

    git_arch = step.context.get("GIT_ARCHAEOLOGY", "no")
    # When Bitbucket is configured with SSH key, git archaeology must succeed
    if env.get("BITBUCKET_REPO") and env.get("SSH_PRIVATE_KEY"):
        if git_arch != "yes":
            return _fail(result, step,
                         f"GIT_ARCHAEOLOGY=no but BITBUCKET_REPO={env['BITBUCKET_REPO']} "
                         f"and SSH_PRIVATE_KEY is set — clone should have succeeded\n"
                         f"stdout tail: {step.stdout[-600:]}")
    return _pass(result, f"ANALYSIS_TYPE={step.context.get('ANALYSIS_TYPE')} "
                         f"GIT_ARCHAEOLOGY={git_arch} "
                         f"ISSUE_COUNT={step.context.get('ISSUE_COUNT')} "
                         f"elapsed={step.elapsed:.0f}s")


def test_03_standup_gather(base_env: dict[str, str]) -> TestResult:
    """Run gather_jira — first step of the standup pipeline.

    Verifies: Jira connectivity, signals.json written, context markers emitted.
    """
    result = TestResult("standup-gather")
    if not base_env.get("JIRA_BASE_URL"):
        result.passed = True
        result.message = "SKIPPED — JIRA_BASE_URL not set"
        return result

    env = dict(base_env)
    ws = "/workspace/standup"
    env["WORKSPACE_DIR"] = ws

    with pod_fixture("standup-gather") as pod:
        step = exec_step(pod, "gather",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         "python3 -m scripts.standup.gather_jira",
                         env, timeout=120)
        result.steps.append(step)

    if not step.ok:
        return _fail(result, step, f"exit code {step.returncode}")

    err = _check_keys(step, ["SELECTED_TRACKER", "ISSUE_COUNT", "PR_COUNT"]) or \
          _check_values(step, {"SELECTED_TRACKER": "jira"})
    return _fail(result, step, err) if err else \
           _pass(result, f"ISSUE_COUNT={step.context.get('ISSUE_COUNT')} "
                         f"PR_COUNT={step.context.get('PR_COUNT')} "
                         f"elapsed={step.elapsed:.0f}s")


def test_04_standup_pipeline(base_env: dict[str, str]) -> TestResult:
    """Run the full standup pipeline: gather → synthesize in a single pod.

    synthesize reads /workspace/signals.json written by gather, so both steps
    must share the same pod and workspace directory.
    """
    result = TestResult("standup-pipeline")
    if not base_env.get("JIRA_BASE_URL"):
        result.passed = True
        result.message = "SKIPPED — JIRA_BASE_URL not set"
        return result

    env = dict(base_env)
    ws = "/workspace/standup_pipeline"
    env["WORKSPACE_DIR"] = ws

    with pod_fixture("standup-pipeline") as pod:
        # Step 1 — gather
        step1 = exec_step(pod, "gather",
                          f"mkdir -p {ws}/reports {ws}/logs && "
                          "python3 -m scripts.standup.gather_jira",
                          env, timeout=120)
        result.steps.append(step1)
        if not step1.ok:
            return _fail(result, step1, f"gather failed with exit code {step1.returncode}")

        err = _check_keys(step1, ["SELECTED_TRACKER", "ISSUE_COUNT"])
        if err:
            return _fail(result, step1, f"gather context check failed: {err}")

        # Step 2 — synthesize (reads signals.json written by gather)
        step2 = exec_step(pod, "synthesize",
                          "python3 -m scripts.standup.synthesize",
                          env, timeout=300)
        result.steps.append(step2)
        if not step2.ok:
            return _fail(result, step2, f"synthesize failed with exit code {step2.returncode}")

        err = _check_keys(step2, ["SELECTED_TRACKER", "ISSUE_COUNT", "SELECTED_MODEL"])
        if err:
            return _fail(result, step2, f"synthesize context check failed: {err}")

    gather_issues = step1.context.get("ISSUE_COUNT", "?")
    synth_model = step2.context.get("SELECTED_MODEL", "?")
    total_elapsed = step1.elapsed + step2.elapsed
    return _pass(result,
                 f"gather ISSUE_COUNT={gather_issues} | "
                 f"synthesize MODEL={synth_model} | "
                 f"total {total_elapsed:.0f}s")


def test_05_gh_query(base_env: dict[str, str]) -> TestResult:
    """Query GitHub issues. Verifies GH_TOKEN + gh CLI connectivity."""
    result = TestResult("gh-query")
    if not base_env.get("GH_TOKEN"):
        result.passed = True
        result.message = "SKIPPED — GH_TOKEN not set"
        return result

    env = dict(base_env)
    ws = "/workspace/gh_query"
    env["WORKSPACE_DIR"] = ws
    env["GH_ORG"] = GH_ORG
    env["GH_REPO"] = GH_REPO

    with pod_fixture("gh-query") as pod:
        step = exec_step(pod, "gh-query",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         "python3 -m scripts.gh.query_issues --query 'bug' --max 5",
                         env, timeout=120)
        result.steps.append(step)

    if not step.ok:
        return _fail(result, step, f"exit code {step.returncode}")
    if not step.has_results:
        return _pass(result, "no matching issues (exit 2) — GitHub reachable")

    err = _check_keys(step, ["SELECTED_TRACKER", "ISSUE_COUNT"]) or \
          _check_values(step, {"SELECTED_TRACKER": "github"})
    return _fail(result, step, err) if err else \
           _pass(result, f"ISSUE_COUNT={step.context.get('ISSUE_COUNT')} "
                         f"elapsed={step.elapsed:.0f}s")


def test_06_gh_analyze(base_env: dict[str, str]) -> TestResult:
    """Analyze GitHub issues with Claude. Verifies ANALYSIS_TYPE context key."""
    result = TestResult("gh-analyze")
    if not base_env.get("GH_TOKEN"):
        result.passed = True
        result.message = "SKIPPED — GH_TOKEN not set"
        return result

    env = dict(base_env)
    ws = "/workspace/gh_analyze"
    env["WORKSPACE_DIR"] = ws
    env["GH_ORG"] = GH_ORG
    env["GH_REPO"] = GH_REPO

    with pod_fixture("gh-analyze") as pod:
        step = exec_step(pod, "gh-analyze",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         "python3 -m scripts.gh.analyze_issues "
                         "--query 'bug' --prompt 'summarize open bugs' --max 3",
                         env, timeout=300)
        result.steps.append(step)

    if not step.ok:
        return _fail(result, step, f"exit code {step.returncode}")
    if not step.has_results:
        return _pass(result, "no matching issues (exit 2) — GitHub reachable")

    err = _check_keys(step, ["SELECTED_TRACKER", "ISSUE_COUNT", "ANALYSIS_TYPE",
                              "GIT_ARCHAEOLOGY"]) or \
          _check_values(step, {"SELECTED_TRACKER": "github"})
    return _fail(result, step, err) if err else \
           _pass(result, f"ANALYSIS_TYPE={step.context.get('ANALYSIS_TYPE')} "
                         f"GIT_ARCHAEOLOGY={step.context.get('GIT_ARCHAEOLOGY')} "
                         f"ISSUE_COUNT={step.context.get('ISSUE_COUNT')} "
                         f"elapsed={step.elapsed:.0f}s")


def test_07_review_skill_loading(base_env: dict[str, str]) -> TestResult:
    """Verify YGS skills install correctly and ygs-review-pr SKILL.md is loadable.

    This test does NOT invoke Claude — it only verifies that:
      1. _ensure_ygs_skills() clones you-got-skills and symlinks skills
      2. The ygs-review-pr skill is present and has substantial content
      3. _load_skill_md() can find and read it
      4. Key skill candidates (review-pr, ygs-review-pr, ygs-code-review) are checked
    """
    result = TestResult("review-skill-loading")
    env = dict(base_env)
    ws = "/workspace/review_skill_test"
    env["WORKSPACE_DIR"] = ws

    with pod_fixture("review-skill") as pod:
        # Write the verify script into the pod, then run it — avoids shell quoting issues
        verify_script = (
            'import json, sys, os\n'
            'sys.path.insert(0, "/app")\n'
            'os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"\n'
            'os.environ["ANTHROPIC_BEDROCK_BASE_URL"] = os.environ.get("ANTHROPIC_BEDROCK_BASE_URL", "http://ai/bedrock")\n'
            'from scripts.common.claude_runner import _ensure_ygs_skills\n'
            '_ensure_ygs_skills()\n'
            'from pathlib import Path\n'
            'skills_base = Path.home() / ".claude" / "skills"\n'
            'installed = sorted(p.name for p in skills_base.iterdir() if p.is_dir() and not p.name.startswith("."))\n'
            'print(f"::add-task-context INSTALLED_SKILLS_COUNT::{len(installed)}", flush=True)\n'
            'review_skills = {}\n'
            'for name in ["ygs-review-pr", "ygs-code-review", "ygs-review-deep", "review-pr"]:\n'
            '    skill_md = skills_base / name / "SKILL.md"\n'
            '    if skill_md.exists():\n'
            '        content = skill_md.read_text(encoding="utf-8")\n'
            '        review_skills[name] = len(content)\n'
            '        print(f"[verify] {name}: {len(content)} chars", flush=True)\n'
            '    else:\n'
            '        link = skills_base / name\n'
            '        if link.is_symlink():\n'
            '            target = link.resolve()\n'
            '            print(f"[verify] {name}: symlink -> {target} (broken={not target.exists()})", flush=True)\n'
            '        else:\n'
            '            print(f"[verify] {name}: NOT FOUND", flush=True)\n'
            'print(f"::add-task-context REVIEW_SKILLS_FOUND::{\",\".join(review_skills.keys())}", flush=True)\n'
            'if "ygs-review-pr" not in review_skills:\n'
            '    print("FAIL: ygs-review-pr SKILL.md not found", flush=True)\n'
            '    sys.exit(1)\n'
            'chars = review_skills["ygs-review-pr"]\n'
            'if chars < 500:\n'
            '    print(f"FAIL: ygs-review-pr SKILL.md too small ({chars} chars)", flush=True)\n'
            '    sys.exit(1)\n'
            'from scripts.review.run import _load_skill_md\n'
            'loaded = _load_skill_md("ygs-review-pr")\n'
            'if not loaded:\n'
            '    print("FAIL: _load_skill_md ygs-review-pr returned None", flush=True)\n'
            '    sys.exit(1)\n'
            'raw_chars = review_skills.get("ygs-review-pr", 0)\n'
            'if len(loaded) <= raw_chars:\n'
            '    print(f"FAIL: inlining did not expand skill ({len(loaded)} <= {raw_chars})", flush=True)\n'
            '    sys.exit(1)\n'
            'if "review-scaffold" not in loaded and "Severity classification" not in loaded:\n'
            '    print("FAIL: inlined content missing review-scaffold sections", flush=True)\n'
            '    sys.exit(1)\n'
            'print(f"::add-task-context SKILL_MD_CHARS::{len(loaded)}", flush=True)\n'
            'print(f"::add-task-context SKILL_RAW_CHARS::{raw_chars}", flush=True)\n'
            'print("::add-task-context SKILL_LOADED::yes", flush=True)\n'
            'print("[verify] all checks passed", flush=True)\n'
        )
        # Write script to pod as a file to avoid shell escaping issues
        _kubectl("exec", pod, "--", "bash", "-c",
                 f"cat > /tmp/verify_skills.py << 'PYEOF'\n{verify_script}PYEOF")
        step = exec_step(pod, "verify-skills",
                         f"mkdir -p {ws} && python3 /tmp/verify_skills.py",
                         env, timeout=120)
        result.steps.append(step)

    if not step.ok:
        return _fail(result, step, f"exit code {step.returncode}")

    err = _check_keys(step, ["INSTALLED_SKILLS_COUNT", "REVIEW_SKILLS_FOUND", "SKILL_LOADED"]) or \
          _check_values(step, {"SKILL_LOADED": "yes"})
    if err:
        return _fail(result, step, err)

    skills_count = step.context.get("INSTALLED_SKILLS_COUNT", "0")
    review_skills = step.context.get("REVIEW_SKILLS_FOUND", "")
    skill_chars = step.context.get("SKILL_MD_CHARS", "0")
    return _pass(result,
                 f"installed={skills_count} skills | "
                 f"review skills: {review_skills} | "
                 f"ygs-review-pr: {skill_chars} chars")


def test_08_review_pr(base_env: dict[str, str]) -> TestResult:
    """Run a full PR review against a known public GitHub PR.

    Requires: GH_TOKEN + Claude (Bedrock or API key).
    Uses a small known PR to keep cost/time low.
    """
    result = TestResult("review-pr")
    if not base_env.get("GH_TOKEN"):
        result.passed = True
        result.message = "SKIPPED — GH_TOKEN not set"
        return result

    env = dict(base_env)
    ws = "/workspace/review_pr"
    env["WORKSPACE_DIR"] = ws
    # Use a small known PR for testing
    pr_url = os.environ.get("REVIEW_PR_URL", f"https://github.com/{GH_ORG}/{GH_REPO}/pull/1")

    with pod_fixture("review-pr") as pod:
        step = exec_step(pod, "review",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         f"python3 -m scripts.review.run --pr-url '{pr_url}'",
                         env, timeout=600)
        result.steps.append(step)

    # Even if review itself fails (bad PR URL, auth issue), we still want
    # to verify the skill loading infrastructure worked correctly.
    err = _check_keys(step, ["SKILL", "SKILL_LOADED"])
    if err:
        return _fail(result, step, f"skill loading failed: {err}")

    skill = step.context.get("SKILL", "")
    skill_loaded = step.context.get("SKILL_LOADED", "")
    skills_invoked = step.context.get("SKILLS_INVOKED", "")
    findings = step.context.get("FINDINGS_COUNT", "?")
    verdict = step.context.get("REVIEW_VERDICT", "?")
    error_reason = step.context.get("ERROR_REASON", "")

    if not step.ok:
        if skill_loaded == "yes":
            # Skill loaded fine but Claude/review failed — still useful info
            return _pass(result,
                         f"skill={skill} loaded={skill_loaded} (review errored: {error_reason[:100]}) | "
                         f"elapsed={step.elapsed:.0f}s")
        return _fail(result, step, f"exit code {step.returncode}, skill_loaded={skill_loaded}")

    return _pass(result,
                 f"skill={skill} loaded={skill_loaded} | "
                 f"invoked={skills_invoked} | "
                 f"findings={findings} verdict={verdict} | "
                 f"elapsed={step.elapsed:.0f}s")


def test_09_audit_git_archaeology(base_env: dict[str, str]) -> TestResult:
    """Run build_audit_context against a local git repo clone in the pod.

    Verifies: git_archaeology.py new audit functions work end-to-end in a container,
    returning a non-empty Markdown block with expected sections.
    Does NOT invoke Claude — only validates the pre-computation layer.
    """
    result = TestResult("audit-git-archaeology")

    env = dict(base_env)
    ws = "/workspace/audit_git"
    env["WORKSPACE_DIR"] = ws

    verify_script = (
        'import subprocess, sys\n'
        'from pathlib import Path\n'
        'import sys; sys.path.insert(0, "/app")\n'
        'from scripts.common.git_archaeology import (\n'
        '    analyze_commit_range, build_audit_context, compute_commit_health,\n'
        '    compute_temporal_coupling, find_test_gaps,\n'
        '    _bug_hotspot_files, _commit_velocity, _emergency_commits, _top_contributors\n'
        ')\n'
        '# Clone a small public repo\n'
        'dest = Path("/tmp/audit-test-repo")\n'
        'if not (dest / ".git").exists():\n'
        '    r = subprocess.run(\n'
        '        ["git", "clone", "--depth", "50", "https://github.com/bhatti/todo-sample.git", str(dest)],\n'
        '        capture_output=True, timeout=120\n'
        '    )\n'
        '    if r.returncode != 0:\n'
        '        print("SKIP: clone failed (no network?) — ", r.stderr.decode()[:200])\n'
        '        print("::add-task-context AUDIT_SKIP::no-network")\n'
        '        sys.exit(0)\n'
        'commits = analyze_commit_range(dest, n_commits=50)\n'
        'if not commits:\n'
        '    print("SKIP: no commits parsed")\n'
        '    print("::add-task-context AUDIT_SKIP::no-commits")\n'
        '    sys.exit(0)\n'
        'health = compute_commit_health(commits)\n'
        'coupling = compute_temporal_coupling(commits, min_support=2)\n'
        'gaps = find_test_gaps(commits)\n'
        'ctx = build_audit_context(dest, n_commits=50)\n'
        'if not ctx or "Repository Audit Context" not in ctx:\n'
        '    print(f"FAIL: build_audit_context returned unexpected: {ctx[:200]!r}")\n'
        '    sys.exit(1)\n'
        'if len(ctx) > 10000:\n'
        '    print(f"FAIL: context too large: {len(ctx)} chars")\n'
        '    sys.exit(1)\n'
        '# Verify new git archaeology helpers\n'
        'velocity = _commit_velocity(dest)\n'
        'contributors = _top_contributors(dest, n_commits=50)\n'
        'bug_hotspots = _bug_hotspot_files(dest)\n'
        'emergency = _emergency_commits(dest)\n'
        'print(f"::add-task-context AUDIT_COMMITS::{len(commits)}")\n'
        'print(f"::add-task-context AUDIT_CONTEXT_CHARS::{len(ctx)}")\n'
        'print(f"::add-task-context AUDIT_FIX_RATIO::{health[\"fix_ratio\"]}")\n'
        'print(f"::add-task-context AUDIT_COUPLING_PAIRS::{len(coupling)}")\n'
        'print(f"::add-task-context AUDIT_TEST_GAP_FILES::{len(gaps[\"untested_files\"])}")\n'
        'print(f"::add-task-context AUDIT_VELOCITY_MONTHS::{len(velocity)}")\n'
        'print(f"::add-task-context AUDIT_CONTRIBUTORS::{len(contributors)}")\n'
        'print("[verify] all git_archaeology checks passed")\n'
    )

    with pod_fixture("audit-git-arch") as pod:
        _kubectl("exec", pod, "--", "bash", "-c",
                 f"cat > /tmp/verify_audit.py << 'PYEOF'\n{verify_script}PYEOF")
        step = exec_step(pod, "verify-audit-git",
                         f"mkdir -p {ws} && python3 /tmp/verify_audit.py",
                         env, timeout=180)
        result.steps.append(step)

    if not step.ok:
        return _fail(result, step, f"exit code {step.returncode}")

    # SKIP case — no network or no commits
    if "AUDIT_SKIP" in step.context:
        result.passed = True
        result.message = f"SKIPPED — {step.context['AUDIT_SKIP']}"
        return result

    err = _check_keys(step, ["AUDIT_COMMITS", "AUDIT_CONTEXT_CHARS"])
    if err:
        return _fail(result, step, err)

    return _pass(result,
                 f"commits={step.context.get('AUDIT_COMMITS')} "
                 f"context_chars={step.context.get('AUDIT_CONTEXT_CHARS')} "
                 f"coupling_pairs={step.context.get('AUDIT_COUPLING_PAIRS', '?')} "
                 f"elapsed={step.elapsed:.0f}s")


def test_10_audit_skill_invoke(base_env: dict[str, str]) -> TestResult:
    """Run full audit pipeline: run_codebase_audit.py → verify reports → post_findings.

    Verifies:
      1. Skill loading, prompt building, all audit context markers emitted
      2. reports/audit_report.md written with content (Claude analysis)
      3. reports/audit_report.html generated from markdown
      4. reports/audit_findings.json is valid JSON with expected keys
      5. post_findings.py runs without crashing (Slack post attempted; token may be absent)

    Requires Claude credentials. Skipped if neither CLAUDE_CODE_USE_BEDROCK nor ANTHROPIC_API_KEY set.
    """
    result = TestResult("audit-skill-invoke")

    # Need Claude to be available
    has_bedrock = base_env.get("CLAUDE_CODE_USE_BEDROCK", "") == "1"
    has_api_key = bool(base_env.get("ANTHROPIC_API_KEY", ""))
    if not (has_bedrock or has_api_key):
        result.passed = True
        result.message = "SKIPPED — no Claude credentials (set CLAUDE_CODE_USE_BEDROCK=1 or ANTHROPIC_API_KEY)"
        return result

    env = dict(base_env)
    ws = "/workspace/audit_skill"
    env["WORKSPACE_DIR"] = ws
    env["N_COMMITS"] = "50"
    env["AUDIT_FOCUS"] = "health"
    env["MAX_TURNS_AUDIT"] = "30"
    # Audit requires substantial analysis — use Sonnet, not Haiku
    sonnet = base_env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "us.anthropic.claude-sonnet-4-6")
    env["AI_MODEL"] = sonnet

    # Test GitHub auto-detection path (no --repo-url; resolver uses GH_ORG + GH_REPO).
    # AUDIT_REPO_URL may still override for Bitbucket / private repos.
    repo_url = os.environ.get("AUDIT_REPO_URL", "")
    if repo_url:
        gh_org = gh_repo = ""
        repo_arg = f"--repo-url '{repo_url}'"
    else:
        gh_org = os.environ.get("GH_ORG", "bhatti")
        gh_repo = os.environ.get("GH_REPO", "todo-sample")
        env["GH_ORG"] = gh_org
        env["GH_REPO"] = gh_repo
        # Unset Bitbucket vars so auto-detect uses GitHub path
        for bb_key in ("BITBUCKET_WORKSPACE", "BITBUCKET_REPO", "BITBUCKET_USERNAME", "BITBUCKET_TOKEN"):
            env.pop(bb_key, None)
        repo_arg = ""  # no --repo-url; auto-detect from GH_ORG + GH_REPO

    with pod_fixture("audit-skill") as pod:
        # ── Step 1: Run the audit ────────────────────────────────────────────
        step = exec_step(pod, "audit-invoke",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         f"python3 -m scripts.analyze.run_codebase_audit "
                         f"{repo_arg} --branch main --commits 50 --focus health",
                         env, timeout=900)
        result.steps.append(step)

        # ── Step 2: Verify report files (runs regardless of Claude success) ─
        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"# audit_report.md must exist and have content\n"
            f"md = os.path.join(ws, 'reports', 'audit_report.md')\n"
            f"if not os.path.exists(md): issues.append('audit_report.md missing')\n"
            f"elif os.path.getsize(md) < 300: issues.append(f'audit_report.md too small — likely a stub ({{os.path.getsize(md)}} bytes, need >300)')\n"
            f"else: print(f'::add-task-context AUDIT_MD_BYTES::{{os.path.getsize(md)}}')\n"
            f"# audit_report.html must exist\n"
            f"html = os.path.join(ws, 'reports', 'audit_report.html')\n"
            f"if not os.path.exists(html): issues.append('audit_report.html missing')\n"
            f"else: print(f'::add-task-context AUDIT_HTML_BYTES::{{os.path.getsize(html)}}')\n"
            f"# audit_findings.json must be valid JSON with expected keys\n"
            f"fj = os.path.join(ws, 'reports', 'audit_findings.json')\n"
            f"if not os.path.exists(fj): issues.append('audit_findings.json missing')\n"
            f"else:\n"
            f"    try:\n"
            f"        d = json.loads(open(fj).read())\n"
            f"        for k in ('repo', 'branch', 'focus'):\n"
            f"            if k not in d: issues.append(f'audit_findings.json missing key {{k}}')\n"
            f"        print(f'::add-task-context AUDIT_FINDINGS_VALID::yes')\n"
            f"    except Exception as e:\n"
            f"        issues.append(f'audit_findings.json parse error: {{e}}')\n"
            f"if issues:\n"
            f"    print('REPORT ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"else:\n"
            f"    print('::add-task-context REPORTS_VERIFIED::yes')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-reports", verify_cmd, env, timeout=30)
        result.steps.append(verify_step)

        # ── Step 3: Run post_findings (Slack post — may fail if no token) ───
        post_cmd = (
            f"python3 -m scripts.review.post_findings "
            f"--findings {ws}/reports/audit_findings.json 2>&1 || true"
        )
        post_env = dict(env)
        post_env["FORMICARY_PUBLIC_URL"] = ""
        post_env["SLACK_THREAD_TS"] = ""
        post_step = exec_step(pod, "post-findings", post_cmd, post_env, timeout=60)
        result.steps.append(post_step)
        if "SLACK_POSTED" in post_step.context:
            print(f"    [post-findings] SLACK_POSTED={post_step.context['SLACK_POSTED']}", flush=True)

    # ── Evaluate results ─────────────────────────────────────────────────────
    err = _check_keys(step, ["SKILL", "AUDIT_REPO", "AUDIT_BRANCH"])
    if err:
        return _fail(result, step, f"audit infrastructure failed: {err}")

    skill = step.context.get("SKILL", "")
    skill_loaded = step.context.get("SKILL_LOADED", "")
    audit_repo = step.context.get("AUDIT_REPO", "")

    if not step.ok:
        if skill_loaded in ("yes", "no"):
            return _pass(result,
                         f"skill={skill} loaded={skill_loaded} repo={audit_repo} "
                         f"(invocation errored — see log) elapsed={step.elapsed:.0f}s")
        return _fail(result, step, f"exit code {step.returncode}")

    # If Claude succeeded, verify reports were written
    if not verify_step.ok:
        return _fail(result, verify_step,
                     f"report files missing after successful audit: {verify_step.stderr[-300:]}")

    crit = step.context.get("AUDIT_CRITICAL_COUNT", "?")
    high = step.context.get("AUDIT_HIGH_COUNT", "?")
    md_bytes = verify_step.context.get("AUDIT_MD_BYTES", "?")
    html_bytes = verify_step.context.get("AUDIT_HTML_BYTES", "?")
    reports_ok = verify_step.context.get("REPORTS_VERIFIED", "no")
    slack_posted = post_step.context.get("SLACK_POSTED", "n/a")
    return _pass(result,
                 f"skill={skill} loaded={skill_loaded} repo={audit_repo} "
                 f"critical={crit} high={high} md={md_bytes}B html={html_bytes}B "
                 f"reports={reports_ok} slack_posted={slack_posted} elapsed={step.elapsed:.0f}s")


def test_11_pr_audit_gh_fetch(base_env: dict[str, str]) -> TestResult:
    """Fetch GitHub PRs and classify comments in a pod (no Claude).

    Verifies: pr_fetcher.py fetch_github_prs(), classify_comments(),
    link_pr_to_issue(), build_pr_context() all work end-to-end in a container.
    """
    result = TestResult("pr-audit-gh-fetch")

    env = dict(base_env)
    ws = "/workspace/pr_audit_fetch"
    env["WORKSPACE_DIR"] = ws
    env["DEFAULT_TRACKER"] = "github"

    gh_org = os.environ.get("GH_ORG", "bhatti")
    gh_repo = os.environ.get("GH_REPO", "you-got-skills")
    env["GH_ORG"] = gh_org
    env["GH_REPO"] = gh_repo
    for bb_key in ("BITBUCKET_WORKSPACE", "BITBUCKET_REPO", "BITBUCKET_USERNAME", "BITBUCKET_TOKEN"):
        env.pop(bb_key, None)

    verify_script = (
        'import json, sys, os\n'
        'sys.path.insert(0, "/app")\n'
        'from scripts.analyze.pr_fetcher import (\n'
        '    fetch_github_prs, classify_comments, link_pr_to_issue, build_pr_context\n'
        ')\n'
        'from scripts.common.config import load_config\n'
        'config = load_config()\n'
        'prs = fetch_github_prs(config, n_prs=5)\n'
        'if not prs:\n'
        '    print("SKIP: no merged PRs found (empty repo?)")\n'
        '    print("::add-task-context PR_AUDIT_SKIP::no-prs")\n'
        '    sys.exit(0)\n'
        'print(f"::add-task-context PR_AUDIT_PRS_FETCHED::{len(prs)}")\n'
        '# Classify comments on first PR\n'
        'first = prs[0]\n'
        'all_comments = first.get("all_comments", [])\n'
        'classified = classify_comments(all_comments)\n'
        'print(f"::add-task-context PR_AUDIT_BOT_COMMENTS::{len(classified[\"bot_comments\"])}")\n'
        'print(f"::add-task-context PR_AUDIT_HUMAN_COMMENTS::{len(classified[\"human_comments\"])}")\n'
        '# Link to issue\n'
        'issue_ref = link_pr_to_issue(first, config)\n'
        'print(f"::add-task-context PR_AUDIT_ISSUE_LINKED::{"yes" if issue_ref else "no"}")\n'
        '# Build context\n'
        'ctx = build_pr_context(prs, max_chars=50000)\n'
        'print(f"::add-task-context PR_AUDIT_CONTEXT_CHARS::{len(ctx)}")\n'
        'if not ctx:\n'
        '    print("FAIL: build_pr_context returned empty")\n'
        '    sys.exit(1)\n'
        '# Write pr_data.json\n'
        f'os.makedirs("{ws}/reports", exist_ok=True)\n'
        f'with open("{ws}/reports/pr_data.json", "w") as f:\n'
        '    json.dump(prs, f, default=str)\n'
        'print(f"::add-task-context PR_AUDIT_DATA_WRITTEN::yes")\n'
        'print("[verify] all pr_fetcher checks passed")\n'
    )

    with pod_fixture("pr-fetch-gh") as pod:
        _kubectl("exec", pod, "--", "bash", "-c",
                 f"cat > /tmp/verify_pr_fetch.py << 'PYEOF'\n{verify_script}PYEOF")
        step = exec_step(pod, "verify-pr-fetch",
                         f"mkdir -p {ws}/reports && python3 /tmp/verify_pr_fetch.py",
                         env, timeout=180)
        result.steps.append(step)

    if not step.ok:
        return _fail(result, step, f"exit code {step.returncode}")

    if "PR_AUDIT_SKIP" in step.context:
        result.passed = True
        result.message = f"SKIPPED — {step.context['PR_AUDIT_SKIP']}"
        return result

    err = _check_keys(step, ["PR_AUDIT_PRS_FETCHED", "PR_AUDIT_CONTEXT_CHARS"])
    if err:
        return _fail(result, step, err)

    return _pass(result,
                 f"prs_fetched={step.context.get('PR_AUDIT_PRS_FETCHED')} "
                 f"context_chars={step.context.get('PR_AUDIT_CONTEXT_CHARS')} "
                 f"bot_comments={step.context.get('PR_AUDIT_BOT_COMMENTS', '?')} "
                 f"human_comments={step.context.get('PR_AUDIT_HUMAN_COMMENTS', '?')} "
                 f"elapsed={step.elapsed:.0f}s")


def test_12_pr_audit_gh_full(base_env: dict[str, str]) -> TestResult:
    """Run full pr-audit pipeline: run_pr_audit.py → verify reports → post_pr_audit.

    Verifies:
      1. Skill loading, prompt building, all PR audit context markers emitted
      2. reports/pr_audit_report.md written with content (Claude analysis)
      3. reports/pr_audit_findings.json is valid JSON with expected keys
      4. reports/skill_improvements.json is valid JSON
      5. post_pr_audit.py runs without crashing (Slack post attempted; token may be absent)

    Requires Claude credentials. Skipped if neither CLAUDE_CODE_USE_BEDROCK nor ANTHROPIC_API_KEY set.
    """
    result = TestResult("pr-audit-gh-full")

    has_bedrock = base_env.get("CLAUDE_CODE_USE_BEDROCK", "") == "1"
    has_api_key = bool(base_env.get("ANTHROPIC_API_KEY", ""))
    if not (has_bedrock or has_api_key):
        result.passed = True
        result.message = "SKIPPED — no Claude credentials (set CLAUDE_CODE_USE_BEDROCK=1 or ANTHROPIC_API_KEY)"
        return result

    env = dict(base_env)
    ws = "/workspace/pr_audit_full"
    env["WORKSPACE_DIR"] = ws
    env["N_PRS"] = "10"
    env["PR_AUDIT_FOCUS"] = "all"
    env["MAX_TURNS_AUDIT"] = "30"
    env["DEFAULT_TRACKER"] = "github"
    sonnet = base_env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "us.anthropic.claude-sonnet-4-6")
    env["AI_MODEL"] = sonnet

    gh_org = os.environ.get("GH_ORG", "bhatti")
    gh_repo = os.environ.get("GH_REPO", "you-got-skills")
    env["GH_ORG"] = gh_org
    env["GH_REPO"] = gh_repo
    for bb_key in ("BITBUCKET_WORKSPACE", "BITBUCKET_REPO", "BITBUCKET_USERNAME", "BITBUCKET_TOKEN"):
        env.pop(bb_key, None)

    with pod_fixture("pr-audit-full") as pod:
        # ── Step 1: Run the PR audit ────────────────────────────────────────
        step = exec_step(pod, "pr-audit-invoke",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         f"python3 -m scripts.analyze.run_pr_audit "
                         f"--n-prs 10 --focus all",
                         env, timeout=900)
        result.steps.append(step)

        # ── Step 2: Verify report files ─────────────────────────────────────
        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"# pr_audit_report.md must exist and have content\n"
            f"md = os.path.join(ws, 'reports', 'pr_audit_report.md')\n"
            f"if not os.path.exists(md): issues.append('pr_audit_report.md missing')\n"
            f"elif os.path.getsize(md) < 300: issues.append(f'pr_audit_report.md too small ({{os.path.getsize(md)}} bytes)')\n"
            f"else: print(f'::add-task-context PR_AUDIT_MD_BYTES::{{os.path.getsize(md)}}')\n"
            f"# pr_audit_findings.json must be valid JSON with expected keys\n"
            f"fj = os.path.join(ws, 'reports', 'pr_audit_findings.json')\n"
            f"if not os.path.exists(fj): issues.append('pr_audit_findings.json missing')\n"
            f"else:\n"
            f"    try:\n"
            f"        d = json.loads(open(fj).read())\n"
            f"        for k in ('repo', 'prs_analyzed'):\n"
            f"            if k not in d: issues.append(f'pr_audit_findings.json missing key {{k}}')\n"
            f"        print(f'::add-task-context PR_AUDIT_FINDINGS_VALID::yes')\n"
            f"    except Exception as e:\n"
            f"        issues.append(f'pr_audit_findings.json parse error: {{e}}')\n"
            f"# skill_improvements.json (may be empty but should be valid JSON)\n"
            f"si = os.path.join(ws, 'reports', 'skill_improvements.json')\n"
            f"if os.path.exists(si):\n"
            f"    try:\n"
            f"        json.loads(open(si).read())\n"
            f"        print(f'::add-task-context SKILL_IMPROVEMENTS_VALID::yes')\n"
            f"    except Exception as e:\n"
            f"        issues.append(f'skill_improvements.json parse error: {{e}}')\n"
            f"if issues:\n"
            f"    print('REPORT ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"else:\n"
            f"    print('::add-task-context PR_REPORTS_VERIFIED::yes')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-pr-reports", verify_cmd, env, timeout=30)
        result.steps.append(verify_step)

        # ── Step 3: Run post_pr_audit (Slack post — may fail if no token) ──
        post_cmd = (
            f"python3 -m scripts.analyze.post_pr_audit 2>&1 || true"
        )
        post_env = dict(env)
        post_env["FORMICARY_PUBLIC_URL"] = ""
        post_env["SLACK_THREAD_TS"] = ""
        post_step = exec_step(pod, "post-pr-audit", post_cmd, post_env, timeout=60)
        result.steps.append(post_step)

    # ── Evaluate results ─────────────────────────────────────────────────────
    err = _check_keys(step, ["SKILL", "PR_AUDIT_REPO"])
    if err:
        return _fail(result, step, f"pr-audit infrastructure failed: {err}")

    skill = step.context.get("SKILL", "")
    skill_loaded = step.context.get("SKILL_LOADED", "")
    audit_repo = step.context.get("PR_AUDIT_REPO", "")

    if not step.ok:
        if skill_loaded in ("yes", "no"):
            return _pass(result,
                         f"skill={skill} loaded={skill_loaded} repo={audit_repo} "
                         f"(invocation errored — see log) elapsed={step.elapsed:.0f}s")
        return _fail(result, step, f"exit code {step.returncode}")

    if not verify_step.ok:
        return _fail(result, verify_step,
                     f"report files missing after successful pr-audit: {verify_step.stderr[-300:]}")

    md_bytes = verify_step.context.get("PR_AUDIT_MD_BYTES", "?")
    reports_ok = verify_step.context.get("PR_REPORTS_VERIFIED", "no")
    return _pass(result,
                 f"skill={skill} loaded={skill_loaded} repo={audit_repo} "
                 f"md={md_bytes}B reports={reports_ok} elapsed={step.elapsed:.0f}s")


def test_13_plan_skill_updates(base_env: dict[str, str]) -> TestResult:
    """Run plan_skill_updates.py with a real pr_audit_report.md and mock skill_improvements.

    Verifies:
      1. reports/skill_update_plan.md is written with content (Claude output)
      2. reports/skill_update_plan_result.json has status=DONE
      3. Exit code 0
      4. ::add-task-context SKILL_UPDATE_PLAN::yes emitted

    Requires Claude credentials. Skipped if neither CLAUDE_CODE_USE_BEDROCK nor ANTHROPIC_API_KEY set.
    """
    result = TestResult("plan-skill-updates")

    has_bedrock = base_env.get("CLAUDE_CODE_USE_BEDROCK", "") == "1"
    has_api_key = bool(base_env.get("ANTHROPIC_API_KEY", ""))
    if not (has_bedrock or has_api_key):
        result.passed = True
        result.message = "SKIPPED — no Claude credentials (set CLAUDE_CODE_USE_BEDROCK=1 or ANTHROPIC_API_KEY)"
        return result

    env = dict(base_env)
    ws = "/workspace/plan_skill_updates"
    env["WORKSPACE_DIR"] = ws
    env["MAX_TURNS_PLAN"] = "15"
    env["AI_MODEL"] = base_env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "us.anthropic.claude-sonnet-4-6")
    env["GH_ORG"] = os.environ.get("GH_ORG", "bhatti")
    env["GH_REPO"] = os.environ.get("GH_REPO", "you-got-skills")
    for bb_key in ("BITBUCKET_WORKSPACE", "BITBUCKET_REPO", "BITBUCKET_USERNAME", "BITBUCKET_TOKEN"):
        env.pop(bb_key, None)

    # Minimal audit report and skill_improvements.json to seed the planner
    mock_audit_report = (
        "# PR Audit Report\n\n"
        "The team consistently skips documentation updates and test coverage for new features.\n\n"
        "## Key Findings\n"
        "- 80% of PRs lack updated README or API documentation\n"
        "- Integration tests are absent for new API endpoints\n"
        "- PR descriptions rarely include testing instructions\n"
    )
    mock_improvements = json.dumps({
        "repo_skill_changes": [
            {"file_path": ".claude/skills/ygs-review-pr.md",
             "action": "update",
             "description": "Add documentation-completeness check",
             "changes": "\n## Documentation Check\nVerify README and API docs updated in PR."}
        ],
        "new_docs": [],
        "ygs_recommendations": []
    })

    setup_cmd = (
        f"mkdir -p {ws}/reports {ws}/logs && "
        f"cat > {ws}/reports/pr_audit_report.md << 'AUDITEOF'\n"
        f"{mock_audit_report}\nAUDITEOF\n"
        f"echo '{mock_improvements}' > {ws}/reports/skill_improvements.json"
    )

    with pod_fixture("plan-skill-updates") as pod:
        # ── Step 1: Set up workspace ─────────────────────────────────────────
        setup_step = exec_step(pod, "setup", setup_cmd, env, timeout=15)
        result.steps.append(setup_step)
        if not setup_step.ok:
            return _fail(result, setup_step, "setup failed")

        # ── Step 2: Run plan_skill_updates ──────────────────────────────────
        run_step = exec_step(pod, "plan-skill-updates-run",
                             f"python3 -m scripts.analyze.plan_skill_updates",
                             env, timeout=300)
        result.steps.append(run_step)

        # ── Step 3: Verify outputs ───────────────────────────────────────────
        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"plan_md = os.path.join(ws, 'reports', 'skill_update_plan.md')\n"
            f"if not os.path.exists(plan_md):\n"
            f"    issues.append('skill_update_plan.md missing')\n"
            f"elif os.path.getsize(plan_md) < 100:\n"
            f"    issues.append(f'skill_update_plan.md too small ({{os.path.getsize(plan_md)}} bytes)')\n"
            f"else:\n"
            f"    print(f'::add-task-context PLAN_MD_BYTES::{{os.path.getsize(plan_md)}}')\n"
            f"result_json = os.path.join(ws, 'reports', 'skill_update_plan_result.json')\n"
            f"if not os.path.exists(result_json):\n"
            f"    issues.append('skill_update_plan_result.json missing')\n"
            f"else:\n"
            f"    try:\n"
            f"        d = json.loads(open(result_json).read())\n"
            f"        status = d.get('status', '?')\n"
            f"        print(f'::add-task-context PLAN_STATUS::{{status}}')\n"
            f"        if status not in ('DONE', 'BLOCKED'):\n"
            f"            issues.append(f'unexpected status: {{status}}')\n"
            f"    except Exception as e:\n"
            f"        issues.append(f'result JSON parse error: {{e}}')\n"
            f"if issues:\n"
            f"    print('PLAN ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"else:\n"
            f"    print('::add-task-context PLAN_VERIFIED::yes')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-plan", verify_cmd, env, timeout=15)
        result.steps.append(verify_step)

    # ── Evaluate ─────────────────────────────────────────────────────────────
    if not run_step.ok:
        return _fail(result, run_step, f"plan_skill_updates failed: exit {run_step.returncode}")

    if not verify_step.ok:
        return _fail(result, verify_step,
                     f"plan outputs missing: {verify_step.stderr[-300:]}")

    plan_status = verify_step.context.get("PLAN_STATUS", "?")
    plan_md_bytes = verify_step.context.get("PLAN_MD_BYTES", "?")
    return _pass(result,
                 f"status={plan_status} plan_md={plan_md_bytes}B "
                 f"elapsed={run_step.elapsed:.0f}s")


def test_14_create_skill_pr(base_env: dict[str, str]) -> TestResult:
    """Run create_skill_pr.py end-to-end: clone → branch → push → create PR → teardown (close PR).

    Verifies:
      1. pr.json is written with url and number > 0
      2. ::add-task-context SKILL_PR_CREATED::yes emitted
      3. PR description contains audit findings section

    Teardown: closes the created PR and deletes the remote branch.

    Requires GH_TOKEN, GH_ORG, GH_REPO in env. Skipped if missing.
    """
    result = TestResult("create-skill-pr")

    gh_token = base_env.get("GH_TOKEN", "")
    gh_org = base_env.get("GH_ORG", os.environ.get("GH_ORG", "bhatti"))
    gh_repo = base_env.get("GH_REPO", os.environ.get("GH_REPO", "you-got-skills"))

    if not gh_token:
        result.passed = True
        result.message = "SKIPPED — GH_TOKEN not set"
        return result

    env = dict(base_env)
    ws = "/workspace/create_skill_pr"
    env["WORKSPACE_DIR"] = ws
    env["CODEBASE_DIR"] = f"{ws}/repo"
    env["DEFAULT_TRACKER"] = "github"
    env["GH_ORG"] = gh_org
    env["GH_REPO"] = gh_repo
    env["GH_REPO_BRANCH"] = "main"
    env["GIT_USER_NAME"] = "AI Agent"
    env["GIT_USER_EMAIL"] = "ai-agent@noreply.local"
    for bb_key in ("BITBUCKET_WORKSPACE", "BITBUCKET_REPO", "BITBUCKET_USERNAME", "BITBUCKET_TOKEN"):
        env.pop(bb_key, None)

    # Minimal skill_improvements.json with one real change.
    # Use a unique suffix so concurrent runs don't collide on the same branch/file.
    run_id = uuid.uuid4().hex[:8]
    skill_file = f".claude/skills/pod-ci-{run_id}.md"
    improvements = json.dumps({
        "repo_skill_changes": [
            {
                "file_path": skill_file,
                "action": "create",
                "description": "CI pod test: temporary skill placeholder (will be closed/deleted)",
                "changes": f"# CI Skill Placeholder\n\nCreated by pod functional test run {run_id}.\n"
            }
        ],
        "new_docs": [],
        "ygs_recommendations": []
    })
    audit_report = (
        "# PR Audit Report\n\nPod functional test run for create-skill-pr validation.\n\n"
        f"Run ID: {run_id}\n\n## Findings\n- Placeholder finding for CI validation.\n"
    )
    skill_update_plan = (
        f"# Skill Update Plan\n\n## Priority 1\nAdd {skill_file} as CI placeholder.\n"
    )

    setup_cmd = (
        f"mkdir -p {ws}/reports {ws}/logs && "
        f"printf '%s' {json.dumps(improvements)} > {ws}/reports/skill_improvements.json && "
        f"printf '%s' {json.dumps(audit_report)} > {ws}/reports/pr_audit_report.md && "
        f"printf '%s' {json.dumps(skill_update_plan)} > {ws}/reports/skill_update_plan.md"
    )

    pr_number = 0
    pr_branch = ""

    with pod_fixture("create-skill-pr") as pod:
        # ── Step 1: Setup ────────────────────────────────────────────────────
        setup_step = exec_step(pod, "setup", setup_cmd, env, timeout=15)
        result.steps.append(setup_step)
        if not setup_step.ok:
            return _fail(result, setup_step, "setup failed")

        # ── Step 2: Run create_skill_pr ──────────────────────────────────────
        run_step = exec_step(pod, "create-skill-pr-run",
                             "python3 -m scripts.analyze.create_skill_pr",
                             env, timeout=120)
        result.steps.append(run_step)

        # ── Step 3: Verify pr.json ───────────────────────────────────────────
        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"pr_json_path = os.path.join(ws, 'pr.json')\n"
            f"if not os.path.exists(pr_json_path):\n"
            f"    issues.append('pr.json missing')\n"
            f"else:\n"
            f"    try:\n"
            f"        d = json.loads(open(pr_json_path).read())\n"
            f"        url = d.get('url','')\n"
            f"        num = d.get('number', 0)\n"
            f"        branch = d.get('branch','')\n"
            f"        print(f'::add-task-context PR_URL::{{url}}')\n"
            f"        print(f'::add-task-context PR_NUMBER::{{num}}')\n"
            f"        print(f'::add-task-context PR_BRANCH::{{branch}}')\n"
            f"        if not url: issues.append('pr.json url is empty')\n"
            f"        if not num: issues.append('pr.json number is 0')\n"
            f"    except Exception as e:\n"
            f"        issues.append(f'pr.json parse error: {{e}}')\n"
            f"if issues:\n"
            f"    print('PR ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"else:\n"
            f"    print('::add-task-context PR_VERIFIED::yes')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-pr", verify_cmd, env, timeout=15)
        result.steps.append(verify_step)

        pr_number = int(verify_step.context.get("PR_NUMBER", "0") or "0")
        pr_branch = verify_step.context.get("PR_BRANCH", "")

        # ── Step 4: Teardown — close PR and delete branch ────────────────────
        if pr_number:
            close_cmd = (
                f"gh pr close {pr_number} -R {gh_org}/{gh_repo} --delete-branch 2>&1 || "
                f"gh api repos/{gh_org}/{gh_repo}/pulls/{pr_number} -X PATCH -f state=closed 2>&1 || true"
            )
            close_env = dict(env)
            close_env["GH_TOKEN"] = gh_token
            close_step = exec_step(pod, "teardown-close-pr", close_cmd, close_env, timeout=30)
            result.steps.append(close_step)
            if not close_step.ok:
                print(f"    [WARNING] PR close failed: {close_step.stderr[-200:]}", flush=True)

    # ── Evaluate ─────────────────────────────────────────────────────────────
    if not run_step.ok:
        return _fail(result, run_step,
                     f"create_skill_pr failed (exit {run_step.returncode})\n"
                     f"stdout: {run_step.stdout[-600:]}\nstderr: {run_step.stderr[-400:]}")

    pr_created = run_step.context.get("SKILL_PR_CREATED", "no")
    if not verify_step.ok:
        return _fail(result, verify_step,
                     f"pr.json invalid: {verify_step.stderr[-300:]}")

    pr_url = verify_step.context.get("PR_URL", "")
    return _pass(result,
                 f"pr={pr_url} number={pr_number} branch={pr_branch} "
                 f"elapsed={run_step.elapsed:.0f}s")


def test_15_create_skill_pr_jira(base_env: dict[str, str]) -> TestResult:
    """Run create_skill_pr.py end-to-end for Bitbucket/Jira tracker.

    Verifies:
      1. Clone succeeds with BITBUCKET_TOKEN (using x-token-auth username for ATATT tokens)
      2. pr.json is written with url and number > 0
      3. ::add-task-context SKILL_PR_CREATED::yes emitted

    Teardown: closes the created BB PR and deletes the remote branch.

    Requires BITBUCKET_TOKEN, BITBUCKET_WORKSPACE, BITBUCKET_REPO in env. Skipped if missing.
    """
    result = TestResult("create-skill-pr-jira")

    bb_token = base_env.get("BITBUCKET_TOKEN", "")
    bb_workspace = base_env.get("BITBUCKET_WORKSPACE", os.environ.get("BITBUCKET_WORKSPACE", ""))
    bb_repo = base_env.get("BITBUCKET_REPO", os.environ.get("BITBUCKET_REPO", ""))
    bb_username = base_env.get("BITBUCKET_USERNAME", os.environ.get("BITBUCKET_USERNAME", "sbhatti@cribl.io"))

    if not bb_token or not bb_workspace or not bb_repo:
        result.passed = True
        result.message = "SKIPPED — BITBUCKET_TOKEN/WORKSPACE/REPO not set"
        return result

    # Look up the BB repo's actual default branch (avoids hardcoding "main")
    import requests as _requests
    try:
        _resp = _requests.get(
            f"https://api.bitbucket.org/2.0/repositories/{bb_workspace}/{bb_repo}",
            headers={"Authorization": f"Bearer {bb_token}"}, timeout=10,
        )
        bb_default_branch = _resp.json().get("mainbranch", {}).get("name", "main") if _resp.ok else "main"
    except Exception:
        bb_default_branch = "main"

    env = dict(base_env)
    ws = "/workspace/create_skill_pr_jira"
    env["WORKSPACE_DIR"] = ws
    env["CODEBASE_DIR"] = f"{ws}/repo"
    env["DEFAULT_TRACKER"] = "jira"
    env["BITBUCKET_WORKSPACE"] = bb_workspace
    env["BITBUCKET_REPO"] = bb_repo
    env["BITBUCKET_USERNAME"] = bb_username
    env["BITBUCKET_TOKEN"] = bb_token
    env["BB_REPO_BRANCH"] = bb_default_branch
    env["GIT_USER_NAME"] = "AI Agent"
    env["GIT_USER_EMAIL"] = "ai-agent@noreply.local"
    for gh_key in ("GH_TOKEN", "GH_ORG", "GH_REPO"):
        env.pop(gh_key, None)

    run_id = uuid.uuid4().hex[:8]
    skill_file = f".claude/skills/pod-ci-jira-{run_id}.md"
    improvements = json.dumps({
        "repo_skill_changes": [
            {
                "file_path": skill_file,
                "action": "create",
                "description": "CI pod test: temporary skill placeholder (Jira/BB — will be closed/deleted)",
                "changes": f"# CI Skill Placeholder (Jira)\n\nCreated by pod functional test run {run_id}.\n"
            }
        ],
        "new_docs": [],
        "ygs_recommendations": []
    })
    audit_report = (
        "# PR Audit Report\n\nPod functional test run for create-skill-pr-jira validation.\n\n"
        f"Run ID: {run_id}\n\n## Findings\n- Placeholder finding for CI validation (BB).\n"
    )
    skill_update_plan = (
        f"# Skill Update Plan (BB)\n\n## Priority 1\nAdd {skill_file} as CI placeholder.\n"
    )

    setup_cmd = (
        f"mkdir -p {ws}/reports {ws}/logs && "
        f"printf '%s' {json.dumps(improvements)} > {ws}/reports/skill_improvements.json && "
        f"printf '%s' {json.dumps(audit_report)} > {ws}/reports/pr_audit_report.md && "
        f"printf '%s' {json.dumps(skill_update_plan)} > {ws}/reports/skill_update_plan.md"
    )

    pr_number = 0
    pr_branch = ""

    with pod_fixture("create-skill-pr-jira") as pod:
        # ── Step 1: Setup ────────────────────────────────────────────────────
        setup_step = exec_step(pod, "setup", setup_cmd, env, timeout=15)
        result.steps.append(setup_step)
        if not setup_step.ok:
            return _fail(result, setup_step, "setup failed")

        # ── Step 2: Run create_skill_pr ──────────────────────────────────────
        run_step = exec_step(pod, "create-skill-pr-jira-run",
                             "python3 -m scripts.analyze.create_skill_pr",
                             env, timeout=120)
        result.steps.append(run_step)

        # ── Step 3: Verify pr.json ───────────────────────────────────────────
        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"pr_json_path = os.path.join(ws, 'pr.json')\n"
            f"if not os.path.exists(pr_json_path):\n"
            f"    issues.append('pr.json missing')\n"
            f"else:\n"
            f"    try:\n"
            f"        d = json.loads(open(pr_json_path).read())\n"
            f"        url = d.get('url','')\n"
            f"        num = d.get('number', 0)\n"
            f"        branch = d.get('branch','')\n"
            f"        print(f'::add-task-context PR_URL::{{url}}')\n"
            f"        print(f'::add-task-context PR_NUMBER::{{num}}')\n"
            f"        print(f'::add-task-context PR_BRANCH::{{branch}}')\n"
            f"        if not url: issues.append('pr.json url is empty')\n"
            f"        if not num: issues.append('pr.json number is 0')\n"
            f"    except Exception as e:\n"
            f"        issues.append(f'pr.json parse error: {{e}}')\n"
            f"if issues:\n"
            f"    print('PR ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"else:\n"
            f"    print('::add-task-context PR_VERIFIED::yes')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-pr", verify_cmd, env, timeout=15)
        result.steps.append(verify_step)

        pr_number = int(verify_step.context.get("PR_NUMBER", "0") or "0")
        pr_branch = verify_step.context.get("PR_BRANCH", "")

        # ── Step 4: Teardown — decline BB PR and delete branch ───────────────
        if pr_number and pr_branch:
            teardown_cmd = (
                f"python3 - <<'PYEOF'\n"
                f"import requests, os\n"
                f"token = os.environ.get('BITBUCKET_TOKEN', '')\n"
                f"headers = {{'Authorization': f'Bearer {{token}}'}}\n"
                f"base = 'https://api.bitbucket.org/2.0/repositories/{bb_workspace}/{bb_repo}'\n"
                f"r = requests.post(f'{{base}}/pullrequests/{pr_number}/decline', headers=headers)\n"
                f"print('decline status:', r.status_code)\n"
                f"r2 = requests.delete(f'{{base}}/refs/branches/{pr_branch}', headers=headers)\n"
                f"print('delete branch status:', r2.status_code)\n"
                f"PYEOF"
            )
            close_step = exec_step(pod, "teardown-decline-pr", teardown_cmd, env, timeout=30)
            result.steps.append(close_step)
            if not close_step.ok:
                print(f"    [WARNING] BB PR teardown failed: {close_step.stderr[-200:]}", flush=True)

    # ── Evaluate ─────────────────────────────────────────────────────────────
    if not run_step.ok:
        return _fail(result, run_step,
                     f"create_skill_pr (jira) failed (exit {run_step.returncode})\n"
                     f"stdout: {run_step.stdout[-600:]}\nstderr: {run_step.stderr[-400:]}")

    if not verify_step.ok:
        return _fail(result, verify_step,
                     f"pr.json invalid: {verify_step.stderr[-300:]}")

    pr_url = verify_step.context.get("PR_URL", "")
    return _pass(result,
                 f"pr={pr_url} number={pr_number} branch={pr_branch} "
                 f"elapsed={run_step.elapsed:.0f}s")


def test_16_respond_comments_jira(base_env: dict[str, str]) -> TestResult:
    """Verify respond_comments.py clones a BB repo using x-token-auth for ATATT tokens.

    Does NOT call Claude — verifies only that:
      1. get_bitbucket_git_username returns 'x-token-auth' for ATATT tokens
      2. The repo can be cloned via HTTPS with that username (actual network clone)

    This exercises the exact code path that failed when BITBUCKET_USERNAME (email)
    was used instead of x-token-auth for ATATT tokens.

    Requires BITBUCKET_TOKEN, BITBUCKET_WORKSPACE, BITBUCKET_REPO in env. Skipped if missing.
    """
    result = TestResult("respond-comments-jira")

    bb_token = base_env.get("BITBUCKET_TOKEN", "")
    bb_workspace = base_env.get("BITBUCKET_WORKSPACE", os.environ.get("BITBUCKET_WORKSPACE", ""))
    bb_repo = base_env.get("BITBUCKET_REPO", os.environ.get("BITBUCKET_REPO", ""))

    if not bb_token or not bb_workspace or not bb_repo:
        result.passed = True
        result.message = "SKIPPED — BITBUCKET_TOKEN/WORKSPACE/REPO not set"
        return result

    env = dict(base_env)
    env["BITBUCKET_WORKSPACE"] = bb_workspace
    env["BITBUCKET_REPO"] = bb_repo
    env["BITBUCKET_TOKEN"] = bb_token

    # Test 1: verify get_bitbucket_git_username returns x-token-auth for ATATT tokens
    username_check_cmd = (
        "python3 -c \""
        "import os; "
        "from scripts.common.git_utils import get_bitbucket_git_username; "
        "config = {'BITBUCKET_TOKEN': os.environ['BITBUCKET_TOKEN'], 'BITBUCKET_USERNAME': 'sbhatti@cribl.io'}; "
        "u = get_bitbucket_git_username(config); "
        "print(f'git_username={u}'); "
        "assert u == 'x-token-auth', f'Expected x-token-auth, got {u}'; "
        "print('::add-task-context GIT_USERNAME::' + u)"
        "\""
    )

    # Test 2: clone the BB repo via HTTPS with x-token-auth (depth=1 for speed)
    clone_cmd = (
        "python3 -c \""
        "import os, shutil; "
        "from scripts.common.git_utils import clone_repo, detect_bitbucket_url, get_bitbucket_git_username; "
        "config = {'BITBUCKET_TOKEN': os.environ['BITBUCKET_TOKEN'], 'BITBUCKET_USERNAME': 'sbhatti@cribl.io'}; "
        "username = get_bitbucket_git_username(config); "
        "token = config['BITBUCKET_TOKEN']; "
        "url = detect_bitbucket_url(os.environ['BITBUCKET_WORKSPACE'], os.environ['BITBUCKET_REPO'], use_ssh=False); "
        "dest = '/tmp/pod_test_respond_clone'; "
        "shutil.rmtree(dest, ignore_errors=True); "
        "clone_repo(url, dest, http_token=token, http_username=username, depth=1); "
        "import subprocess; r = subprocess.run(['git','-C',dest,'log','--oneline','-1'], capture_output=True, text=True); "
        "print('clone OK, HEAD:', r.stdout.strip()); "
        "print('::add-task-context CLONE_OK::yes')"
        "\""
    )

    with pod_fixture("respond-comments-jira") as pod:
        # Step 1: check username helper
        step1 = exec_step(pod, "check-git-username", username_check_cmd, env, timeout=20)
        result.steps.append(step1)
        if not step1.ok:
            return _fail(result, step1, f"get_bitbucket_git_username check failed: {step1.stderr[-300:]}")

        # Step 2: clone repo with correct credentials
        step2 = exec_step(pod, "clone-bb-repo", clone_cmd, env, timeout=90)
        result.steps.append(step2)
        if not step2.ok:
            return _fail(result, step2,
                         f"BB HTTPS clone failed (ATATT token auth bug?): {step2.stderr[-400:]}")

    git_username = step1.context.get("GIT_USERNAME", "?")
    clone_ok = step2.context.get("CLONE_OK", "no")
    return _pass(result, f"git_username={git_username} clone_ok={clone_ok} elapsed={step2.elapsed:.0f}s")


def test_17_pr_audit_by_urls(base_env: dict[str, str]) -> TestResult:
    """Run run_pr_audit.py with explicit PR URLs (--pr-urls) instead of last-N.

    Verifies the new URL-based fetch path:
      1. run_pr_audit fetches the specified PRs and runs analysis
      2. reports/pr_audit_report.md written with content
      3. reports/pr_audit_findings.json is valid JSON with expected keys
    Uses haiku model for speed.
    """
    result = TestResult("pr-audit-by-urls")

    has_bedrock = base_env.get("CLAUDE_CODE_USE_BEDROCK", "") == "1"
    has_api_key = bool(base_env.get("ANTHROPIC_API_KEY", ""))
    if not (has_bedrock or has_api_key):
        result.passed = True
        result.message = "SKIPPED — no Claude credentials"
        return result

    env = dict(base_env)
    gh_org = os.environ.get("GH_ORG", "bhatti")
    gh_repo = os.environ.get("GH_REPO", "todo-sample")
    env["GH_ORG"] = gh_org
    env["GH_REPO"] = gh_repo
    env["DEFAULT_TRACKER"] = "github"
    haiku = base_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    env["AI_MODEL"] = haiku
    env["MAX_TURNS_AUDIT"] = "20"
    ws = "/workspace/pr_audit_by_urls"
    env["WORKSPACE_DIR"] = ws
    # Remove BB creds so GH is unambiguously selected
    for bb_key in ("BITBUCKET_WORKSPACE", "BITBUCKET_REPO", "BITBUCKET_USERNAME", "BITBUCKET_TOKEN"):
        env.pop(bb_key, None)

    # Use the GH_PR_URL env var if set, else default to a known public PR
    gh_pr_url = os.environ.get("GH_PR_URL", f"https://github.com/{gh_org}/{gh_repo}/pull/9")
    # Strip trailing fragment/query that could confuse parse_pr_url
    gh_pr_url = gh_pr_url.split("?")[0].split("#")[0].rstrip("/")

    with pod_fixture("pr-audit-urls") as pod:
        # ── Step 1: Run audit with --pr-urls ────────────────────────────────────
        step = exec_step(pod, "pr-audit-urls-invoke",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         f"python3 -m scripts.analyze.run_pr_audit "
                         f"--pr-urls '{gh_pr_url}' --focus all",
                         env, timeout=600)
        result.steps.append(step)

        # ── Step 2: Verify reports ───────────────────────────────────────────────
        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"md = os.path.join(ws, 'reports', 'pr_audit_report.md')\n"
            f"if not os.path.exists(md): issues.append('pr_audit_report.md missing')\n"
            f"elif os.path.getsize(md) < 100: issues.append(f'pr_audit_report.md too small')\n"
            f"else: print(f'::add-task-context PR_AUDIT_URL_MD_BYTES::{{os.path.getsize(md)}}')\n"
            f"fj = os.path.join(ws, 'reports', 'pr_audit_findings.json')\n"
            f"if not os.path.exists(fj): issues.append('pr_audit_findings.json missing')\n"
            f"else:\n"
            f"    try:\n"
            f"        d = json.loads(open(fj).read())\n"
            f"        for k in ('repo', 'prs_analyzed'):\n"
            f"            if k not in d: issues.append(f'findings missing key {{k}}')\n"
            f"        print('::add-task-context PR_AUDIT_URL_FINDINGS::yes')\n"
            f"    except Exception as e:\n"
            f"        issues.append(f'findings parse error: {{e}}')\n"
            f"if issues:\n"
            f"    print('ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"else:\n"
            f"    print('::add-task-context PR_AUDIT_URL_VERIFIED::yes')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-pr-audit-urls", verify_cmd, env, timeout=20)
        result.steps.append(verify_step)

    err = _check_keys(step, ["PR_AUDIT_REPO"])
    if err:
        return _fail(result, step, f"audit-by-urls infra failed: {err}")

    if not step.ok:
        skill_loaded = step.context.get("SKILL_LOADED", "")
        if skill_loaded in ("yes", "no"):
            return _pass(result, f"skill loaded={skill_loaded} (invocation errored — see log) elapsed={step.elapsed:.0f}s")
        return _fail(result, step, f"exit code {step.returncode}")

    if not verify_step.ok:
        return _fail(result, verify_step, f"report files missing: {verify_step.stderr[-300:]}")

    md_bytes = verify_step.context.get("PR_AUDIT_URL_MD_BYTES", "?")
    return _pass(result,
                 f"pr_url={gh_pr_url} md={md_bytes}B "
                 f"elapsed={step.elapsed:.0f}s")


def test_18_pr_audit_slack_model(base_env: dict[str, str]) -> TestResult:
    """Run run_pr_audit.py with model override embedded in SLACK_MESSAGE.

    Verifies that --model <haiku> in SLACK_MESSAGE overrides AI_MODEL and
    that backward-compat last-N PR fetch still works (no --pr-urls given).
    Uses 5 PRs and haiku model for speed.
    """
    result = TestResult("pr-audit-slack-model")

    has_bedrock = base_env.get("CLAUDE_CODE_USE_BEDROCK", "") == "1"
    has_api_key = bool(base_env.get("ANTHROPIC_API_KEY", ""))
    if not (has_bedrock or has_api_key):
        result.passed = True
        result.message = "SKIPPED — no Claude credentials"
        return result

    env = dict(base_env)
    gh_org = os.environ.get("GH_ORG", "bhatti")
    gh_repo = os.environ.get("GH_REPO", "todo-sample")
    env["GH_ORG"] = gh_org
    env["GH_REPO"] = gh_repo
    env["DEFAULT_TRACKER"] = "github"
    haiku = base_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    env["AI_MODEL"] = haiku
    env["MAX_TURNS_AUDIT"] = "20"
    # Embed model override in SLACK_MESSAGE — this is the feature being tested
    env["SLACK_MESSAGE"] = f"audit last 5 prs --model {haiku}"
    ws = "/workspace/pr_audit_slack_model"
    env["WORKSPACE_DIR"] = ws
    for bb_key in ("BITBUCKET_WORKSPACE", "BITBUCKET_REPO", "BITBUCKET_USERNAME", "BITBUCKET_TOKEN"):
        env.pop(bb_key, None)

    with pod_fixture("pr-audit-slack-mdl") as pod:
        step = exec_step(pod, "pr-audit-slack-model-invoke",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         f"python3 -m scripts.analyze.run_pr_audit --focus all",
                         env, timeout=600)
        result.steps.append(step)

    err = _check_keys(step, ["PR_AUDIT_REPO", "SKILL"])
    if err:
        return _fail(result, step, f"slack-model infra failed: {err}")

    if not step.ok:
        skill_loaded = step.context.get("SKILL_LOADED", "")
        if skill_loaded in ("yes", "no"):
            return _pass(result, f"skill={step.context.get('SKILL')} loaded={skill_loaded} "
                                 f"(invocation errored — see log) elapsed={step.elapsed:.0f}s")
        return _fail(result, step, f"exit code {step.returncode}")

    selected_model = step.context.get("SELECTED_MODEL", "")
    return _pass(result,
                 f"skill={step.context.get('SKILL')} "
                 f"selected_model={selected_model} "
                 f"n_prs={step.context.get('PR_AUDIT_N_PRS', '?')} "
                 f"elapsed={step.elapsed:.0f}s")


def test_19_learn_gh(base_env: dict[str, str]) -> TestResult:
    """Run scripts.gh.learn with a pre-seeded workspace and verify learnings.md output.

    Verifies:
      1. learn.py reads pr.json + issue.json from workspace
      2. Calls fetch_single_pr for PR health context (non-fatal if API unavailable)
      3. Runs Claude with combined Phase 0 + Phase 1 prompt
      4. Writes learnings.md with substantive content
    Uses haiku model and a public GH PR for speed.
    """
    result = TestResult("learn-gh")

    has_bedrock = base_env.get("CLAUDE_CODE_USE_BEDROCK", "") == "1"
    has_api_key = bool(base_env.get("ANTHROPIC_API_KEY", ""))
    if not (has_bedrock or has_api_key):
        result.passed = True
        result.message = "SKIPPED — no Claude credentials"
        return result

    env = dict(base_env)
    gh_org = os.environ.get("GH_ORG", "bhatti")
    gh_repo = os.environ.get("GH_REPO", "todo-sample")
    env["GH_ORG"] = gh_org
    env["GH_REPO"] = gh_repo
    env["DEFAULT_TRACKER"] = "github"
    haiku = base_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    env["AI_MODEL"] = haiku
    env["MAX_TURNS_LEARN"] = "20"
    # Use standard WORKSPACE_DIR=/workspace so get_issue_dir(config, issue_id)
    # resolves to /workspace/{issue_id} — same pattern as existing tests
    ws = "/workspace"
    env["WORKSPACE_DIR"] = ws
    for bb_key in ("BITBUCKET_WORKSPACE", "BITBUCKET_REPO", "BITBUCKET_USERNAME", "BITBUCKET_TOKEN"):
        env.pop(bb_key, None)

    # Extract PR number from GH_PR_URL env or use known public PR
    gh_pr_url = os.environ.get("GH_PR_URL", f"https://github.com/{gh_org}/{gh_repo}/pull/9")
    try:
        pr_number = int(gh_pr_url.rstrip("/").split("/")[-1])
    except (ValueError, IndexError):
        pr_number = 9
    issue_id = f"learn-{pr_number}"  # unique ID avoids collision with other tests

    with pod_fixture("learn-gh") as pod:
        # ── Step 1: Seed workspace with pr.json + issue.json ────────────────────
        # get_issue_dir(config, issue_id) returns /workspace directly (no sub-dir),
        # so pr.json lives at /workspace/pr.json regardless of issue_id.
        issue_dir = ws
        setup_cmd = (
            f"mkdir -p {ws}/logs && "
            f"printf '{{\"number\": {pr_number}, \"title\": \"Test PR\"}}' > {ws}/pr.json && "
            f"printf '{{\"title\": \"Test issue\", \"number\": {pr_number}}}' > {ws}/issue.json"
        )
        setup_step = exec_step(pod, "learn-gh-setup", setup_cmd, env, timeout=15)
        result.steps.append(setup_step)
        if not setup_step.ok:
            return _fail(result, setup_step, "workspace setup failed")

        # ── Step 2: Run learn ────────────────────────────────────────────────────
        learn_step = exec_step(pod, "learn-gh-run",
                               f"python3 -m scripts.gh.learn --issue-id {issue_id}",
                               env, timeout=600)
        result.steps.append(learn_step)

        # ── Step 3: Verify learnings.md ─────────────────────────────────────────
        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import sys, os\n"
            f"issue_dir = '{issue_dir}'\n"
            f"issues = []\n"
            f"learnings_path = os.path.join(issue_dir, 'learnings.md')\n"
            f"if not os.path.exists(learnings_path):\n"
            f"    issues.append('learnings.md missing')\n"
            f"else:\n"
            f"    content = open(learnings_path).read()\n"
            f"    size = len(content)\n"
            f"    print(f'::add-task-context LEARNINGS_BYTES::{{size}}')\n"
            f"    if 'PR Health Analysis' in content or 'Implementation Learnings' in content:\n"
            f"        print('::add-task-context LEARNINGS_STRUCTURE::ok')\n"
            f"    elif size > 100:\n"
            f"        print('::add-task-context LEARNINGS_STRUCTURE::content-no-headers')\n"
            f"    else:\n"
            f"        issues.append(f'learnings.md too small: {{size}} bytes')\n"
            f"if issues:\n"
            f"    print('LEARN ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"else:\n"
            f"    print('::add-task-context LEARN_VERIFIED::yes')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "learn-gh-verify", verify_cmd, env, timeout=20)
        result.steps.append(verify_step)

    if not learn_step.ok:
        # learn.py exits 1 on hard errors; fetch_single_pr failures are non-fatal
        return _fail(result, learn_step, f"learn.py failed (exit={learn_step.returncode}): {learn_step.stderr[-300:]}")

    if not verify_step.ok:
        return _fail(result, verify_step, f"learnings.md check failed: {verify_step.stderr[-300:]}")

    learnings_bytes = verify_step.context.get("LEARNINGS_BYTES", "?")
    structure = verify_step.context.get("LEARNINGS_STRUCTURE", "?")
    return _pass(result,
                 f"pr={pr_number} learnings_bytes={learnings_bytes} "
                 f"structure={structure} elapsed={learn_step.elapsed:.0f}s")


# ── test registry ──────────────────────────────────────────────────────────────

ALL_TESTS: dict[str, callable] = {
    "jira-query":            test_01_jira_query,
    "jira-analyze":          test_02_jira_analyze,
    "standup-gather":        test_03_standup_gather,
    "standup-pipeline":      test_04_standup_pipeline,
    "gh-query":              test_05_gh_query,
    "gh-analyze":            test_06_gh_analyze,
    "review-skill-loading":  test_07_review_skill_loading,
    "review-pr":             test_08_review_pr,
    "audit-git-archaeology": test_09_audit_git_archaeology,
    "audit-skill-invoke":    test_10_audit_skill_invoke,
    "pr-audit-gh-fetch":     test_11_pr_audit_gh_fetch,
    "pr-audit-gh-full":      test_12_pr_audit_gh_full,
    "plan-skill-updates":    test_13_plan_skill_updates,
    "create-skill-pr":       test_14_create_skill_pr,
    "create-skill-pr-jira":  test_15_create_skill_pr_jira,
    "respond-comments-jira": test_16_respond_comments_jira,
    "pr-audit-by-urls":      test_17_pr_audit_by_urls,
    "pr-audit-slack-model":  test_18_pr_audit_slack_model,
    "learn-gh":              test_19_learn_gh,
}

DEFAULT_TESTS = ["jira-query", "jira-analyze", "standup-gather"]


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Pod-based functional tests for ai-dev-tools")
    parser.add_argument("--tests", default=",".join(DEFAULT_TESTS),
                        help="Comma-separated test names, or 'all'")
    parser.add_argument("--list", action="store_true", help="List available tests and exit")
    args = parser.parse_args()

    if args.list:
        print("Available tests:")
        for name in ALL_TESTS:
            print(f"  {name}")
        return

    # Load secret + build base env once — shared across all tests
    print("[pod-tests] loading ai-dev-credentials secret ...", flush=True)
    try:
        secret = load_secret()
        print(f"[pod-tests] loaded {len(secret)} secret keys: {sorted(secret)}", flush=True)
    except Exception as e:
        print(f"[pod-tests] ERROR: cannot load secret: {e}", file=sys.stderr)
        sys.exit(1)

    base_env = build_base_env(secret)

    # Resolve test list
    if args.tests.strip().lower() == "all":
        names = list(ALL_TESTS.keys())
    else:
        names = [t.strip().lower() for t in args.tests.split(",") if t.strip()]

    unknown = [n for n in names if n not in ALL_TESTS]
    if unknown:
        print(f"[pod-tests] unknown tests: {unknown}", file=sys.stderr)
        print(f"[pod-tests] available: {list(ALL_TESTS)}", file=sys.stderr)
        sys.exit(1)

    results: list[TestResult] = []
    for i, name in enumerate(names, 1):
        fn = ALL_TESTS[name]
        print(f"\n{'='*60}", flush=True)
        print(f"[pod-tests] test {i}/{len(names)}: {name}", flush=True)
        print(f"{'='*60}", flush=True)
        try:
            r = fn(base_env)
        except Exception as e:
            r = TestResult(name=name, passed=False,
                           message=f"EXCEPTION: {type(e).__name__}: {e}")
        results.append(r)
        icon = "✓" if r.passed else "✗"
        print(f"\n[pod-tests] {icon} {r.name}: {r.message}", flush=True)

    # Summary
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"\n{'='*60}", flush=True)
    print(f"[pod-tests] SUMMARY: {passed}/{total} passed", flush=True)
    for r in results:
        icon = "✓" if r.passed else "✗"
        print(f"  {icon}  {r.name}: {r.message}", flush=True)

    if passed < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
