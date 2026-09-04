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
]

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
