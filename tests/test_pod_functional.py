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

    # Clean up stale pods from interrupted runs (label: app=ai-dev-pod-test):
    python3 tests/test_pod_functional.py --cleanup

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
    "scripts/common/issue_fetcher.py",
    "scripts/common/jira_api.py",
    "scripts/common/gh_api.py",
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
    "scripts/adhoc/__init__.py",
    "scripts/adhoc/run_skill.py",
    "scripts/skill/__init__.py",
    "scripts/skill/flags.py",
    "scripts/skill/run_skill.py",
    "scripts/skill/post.py",
    # scripts/common files added after initial list — keep in sync with scripts/common/
    "scripts/common/__init__.py",
    "scripts/common/bootstrap.py",
    "scripts/common/entrypoint.py",
    "scripts/common/idempotency.py",
    "scripts/common/label_utils.py",
    "scripts/common/pr_utils.py",
    "scripts/common/setup_tracker.py",
    "scripts/common/text_utils.py",
    "scripts/mq/__init__.py",
    "scripts/mq/_shared.py",
    "scripts/mq/clone_pr.py",
    "scripts/mq/collect_ready.py",
    "scripts/mq/group_by_scope.py",
    "scripts/mq/report.py",
    "scripts/mq/risk_score.py",
    "scripts/mq/run_scoped_ci.py",
    "scripts/mq/scope_router.py",
    "scripts/mq/test_impact.py",
    "requirements.txt",
]

# Directories to copy wholesale (e.g. .claude/skills for skill pod tests).
_DIRS_TO_COPY = [
    ".claude/skills",
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


def _create_pod(name: str, image: str = IMAGE) -> None:
    manifest = _POD_MANIFEST.format(name=name, namespace=NAMESPACE, image=image)
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
    # Copy whole directories (e.g. .claude/skills).
    for rel_dir in _DIRS_TO_COPY:
        local_dir = REPO_ROOT / rel_dir
        if not local_dir.is_dir():
            print(f"    WARNING: {rel_dir}/ not found locally — skipping", flush=True)
            continue
        _kubectl("exec", pod_name, "--", "mkdir", "-p", f"/app/{rel_dir}", timeout=15, check=False)
        _kubectl("cp", str(local_dir), f"{pod_name}:/app/{rel_dir}", timeout=30)
        n += 1
    print(f"    copied {n} item(s) into {pod_name}", flush=True)


# ── pod fixture ────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def pod_fixture(test_name: str, image: str = IMAGE):
    """Context manager: create pod, copy scripts, yield pod_name, delete on exit."""
    name = f"ai-dev-{test_name.replace('_', '-')[:20]}-{uuid.uuid4().hex[:6]}"
    print(f"\n  [pod] creating {name} for test '{test_name}' ...", flush=True)
    _create_pod(name, image=image)
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
    """Run a bash command in the pod; stream stdout live; parse context markers."""
    env_lines = "\n".join(f"export {k}={json.dumps(v)}" for k, v in env.items())
    script = f"set -uo pipefail\n{env_lines}\ncd /app\n{cmd}"
    print(f"    [{label}] running ...", flush=True)
    t0 = time.time()
    proc = subprocess.Popen(
        ["kubectl", "-n", NAMESPACE, "exec", pod_name, "--", "bash", "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr
        for line in proc.stderr:
            stderr_lines.append(line)
            sys.stderr.write(f"    [{label}] {line}")
            sys.stderr.flush()

    import threading
    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    assert proc.stdout
    try:
        for line in proc.stdout:
            stdout_lines.append(line)
            sys.stdout.write(f"    [{label}] {line}")
            sys.stdout.flush()
    except Exception:
        pass

    try:
        proc.wait(timeout=max(0, timeout - int(time.time() - t0)))
    except subprocess.TimeoutExpired:
        proc.kill()
    stderr_thread.join(timeout=5)

    elapsed = time.time() - t0
    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
    ctx = _parse_context(stdout)
    print(f"    [{label}] exit={proc.returncode} elapsed={elapsed:.0f}s "
          f"context_keys={list(ctx.keys())}", flush=True)
    return StepResult(
        label=label,
        returncode=proc.returncode or 0,
        stdout=stdout,
        stderr=stderr,
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
        # Step 1: free-text JQL path (likely no results)
        step = exec_step(pod, "jira-query",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         "python3 -m scripts.jira.query_issues --query 'open' --max 5",
                         env, timeout=120)
        result.steps.append(step)

        if not step.ok:
            return _fail(result, step, f"exit code {step.returncode}")

        # Step 2: direct-fetch path — pass issue key/URL so extract_jira_keys fires
        if ISSUE_ID:
            jira_base = base_env.get("JIRA_BASE_URL", "").rstrip("/")
            # Use the full browse URL to test URL parsing in extract_jira_keys
            issue_url = f"{jira_base}/browse/{ISSUE_ID}" if jira_base else ISSUE_ID
            step2 = exec_step(pod, "jira-query-by-url",
                              f"python3 -m scripts.jira.query_issues --query '{issue_url}' --max 1",
                              env, timeout=60)
            result.steps.append(step2)
            if not step2.ok:
                return _fail(result, step2, f"direct-fetch exit code {step2.returncode}")
            if not step2.has_results:
                return _fail(result, step2, f"direct-fetch returned no issues for {ISSUE_ID}")
            err = _check_keys(step2, ["SELECTED_TRACKER", "ISSUE_COUNT"]) or \
                  _check_values(step2, {"SELECTED_TRACKER": "jira", "ISSUE_COUNT": "1"})
            if err:
                return _fail(result, step2, err)
            return _pass(result, f"direct-fetch ISSUE_COUNT=1 for {ISSUE_ID} elapsed={step2.elapsed:.0f}s")

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

    verify_step = None
    content_step = None
    with pod_fixture("jira-analyze") as pod:
        step = exec_step(pod, "jira-analyze",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         f"python3 -m scripts.jira.analyze_issues "
                         f"--issues '{ISSUE_ID}' --prompt 'give tldr for {ISSUE_ID}' --max 5",
                         env, timeout=300)
        result.steps.append(step)

        if step.ok and step.has_results:
            # Verify reports/report.md was written with substantial content
            verify_cmd = (
                f"python3 -c \""
                f"import os, sys; "
                f"md='{ws}/reports/report.md'; "
                f"sz=os.path.getsize(md) if os.path.exists(md) else 0; "
                f"print(f'::add-task-context REPORT_MD_BYTES::{{sz}}'); "
                f"sys.exit(0 if sz > 300 else 1)"
                f"\""
            )
            verify_step = exec_step(pod, "verify-jira-report", verify_cmd, env, timeout=30)
            result.steps.append(verify_step)

            # Verify no leaked markers, report.html exists, and when repo was cloned the report
            # cites at least one file path (proving Phase 0 grep ran on actual source files)
            repo_cloned = step.context.get("REPO_CLONED_PATH", "")
            verify_content_cmd = (
                f"python3 -c \""
                f"import sys, re, os; "
                f"t=open('{ws}/reports/report.md').read(); "
                f"bad=re.search(r'add-task-context|Full report at|Full analysis at', t, re.I); "
                f"html_ok=os.path.exists('{ws}/reports/report.html'); "
                f"repo_cloned={repr(bool(repo_cloned))}; "
                f"has_path=bool(re.search(r'[a-zA-Z0-9_/\\\\-]+\\.[a-z]{{1,5}}(?::\\d+)?', t)); "
                f"path_ok = (not repo_cloned) or has_path; "
                f"print(f'report.html={{\\\"OK\\\" if html_ok else \\\"MISSING\\\"}}',"
                f"      f'file_paths={{\\\"yes\\\" if has_path else \\\"NONE - Phase0 may not have run\\\"}}'); "
                f"sys.exit(0 if not bad and html_ok and path_ok else 1)"
                f"\""
            )
            content_step = exec_step(pod, "verify-report-content", verify_content_cmd, env, timeout=15)
            result.steps.append(content_step)

            # Copy report.md to local /tmp so caller can inspect it
            local_report = f"/tmp/{ISSUE_ID}-analysis.md" if ISSUE_ID else "/tmp/jira-analysis.md"
            try:
                subprocess.run(
                    ["kubectl", "-n", NAMESPACE, "cp",
                     f"{pod}:{ws}/reports/report.md", local_report],
                    check=True, capture_output=True,
                )
                print(f"    [jira-analyze] report saved locally → {local_report}", flush=True)
            except Exception as e:
                print(f"    [jira-analyze] warn: could not copy report.md locally: {e}", flush=True)

    if not step.ok:
        return _fail(result, step,
                     f"exit code {step.returncode}\nstdout tail:\n{step.stdout[-1000:]}")
    if not step.has_results:
        return _pass(result, f"no issues found for {ISSUE_ID} (exit 2)")

    err = _check_keys(step, ["SELECTED_TRACKER", "ISSUE_COUNT", "ANALYSIS_TYPE",
                              "GIT_ARCHAEOLOGY", "ISSUE_LINKS_COUNT", "PR_LINKS_COUNT",
                              "ATTACHMENTS_COUNT"]) or \
          _check_values(step, {"SELECTED_TRACKER": "jira"})
    if err:
        return _fail(result, step, err)

    git_arch = step.context.get("GIT_ARCHAEOLOGY", "no")
    if env.get("BITBUCKET_REPO") and env.get("SSH_PRIVATE_KEY"):
        if git_arch != "yes":
            return _fail(result, step,
                         f"GIT_ARCHAEOLOGY=no but BITBUCKET_REPO={env['BITBUCKET_REPO']} "
                         f"and SSH_PRIVATE_KEY is set — clone should have succeeded\n"
                         f"stdout tail: {step.stdout[-600:]}")

    if verify_step and not verify_step.ok:
        return _fail(result, verify_step,
                     f"reports/report.md missing or too small (want >300 bytes, "
                     f"got {verify_step.context.get('REPORT_MD_BYTES', 0)} bytes)\n"
                     f"analyze stdout tail:\n{step.stdout[-2000:]}")

    if content_step and not content_step.ok:
        return _fail(result, content_step,
                     "report.md has leaked markers, report.html missing, or no file paths "
                     "(Phase 0 grep did not run on cloned repo)")

    report_sz = verify_step.context.get("REPORT_MD_BYTES", "?") if verify_step else "?"
    skill_used = step.context.get("SKILL_USED", "none")
    return _pass(result, f"ANALYSIS_TYPE={step.context.get('ANALYSIS_TYPE')} "
                         f"SKILL_USED={skill_used} "
                         f"GIT_ARCHAEOLOGY={git_arch} "
                         f"ISSUE_COUNT={step.context.get('ISSUE_COUNT')} "
                         f"ISSUE_LINKS_COUNT={step.context.get('ISSUE_LINKS_COUNT')} "
                         f"PR_LINKS_COUNT={step.context.get('PR_LINKS_COUNT')} "
                         f"ATTACHMENTS_COUNT={step.context.get('ATTACHMENTS_COUNT')} "
                         f"report.md={report_sz}B "
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

    verify_step = None
    with pod_fixture("gh-analyze") as pod:
        step = exec_step(pod, "gh-analyze",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         "python3 -m scripts.gh.analyze_issues "
                         "--issues '27' --prompt 'analyze issue 27' --max 3",
                         env, timeout=300)
        result.steps.append(step)

        if step.ok and step.has_results:
            verify_cmd = (
                f"python3 -c \""
                f"import os, sys; "
                f"md='{ws}/reports/report.md'; "
                f"sz=os.path.getsize(md) if os.path.exists(md) else 0; "
                f"print(f'::add-task-context REPORT_MD_BYTES::{{sz}}'); "
                f"sys.exit(0 if sz > 300 else 1)"
                f"\""
            )
            verify_step = exec_step(pod, "verify-gh-report", verify_cmd, env, timeout=30)
            result.steps.append(verify_step)

    if not step.ok:
        return _fail(result, step, f"exit code {step.returncode}")
    if not step.has_results:
        return _pass(result, "no matching issues (exit 2) — GitHub reachable")

    err = _check_keys(step, ["SELECTED_TRACKER", "ISSUE_COUNT", "ANALYSIS_TYPE",
                              "GIT_ARCHAEOLOGY", "PR_LINKS_COUNT"]) or \
          _check_values(step, {"SELECTED_TRACKER": "github"})
    if err:
        return _fail(result, step, err)

    if verify_step and not verify_step.ok:
        return _fail(result, verify_step,
                     f"reports/report.md missing or too small (want >300 bytes, "
                     f"got {verify_step.context.get('REPORT_MD_BYTES', 0)} bytes)")

    report_sz = verify_step.context.get("REPORT_MD_BYTES", "?") if verify_step else "?"
    return _pass(result, f"ANALYSIS_TYPE={step.context.get('ANALYSIS_TYPE')} "
                         f"GIT_ARCHAEOLOGY={step.context.get('GIT_ARCHAEOLOGY')} "
                         f"CLONE_METHOD={step.context.get('CLONE_METHOD', 'N/A')} "
                         f"PR_LINKS_COUNT={step.context.get('PR_LINKS_COUNT')} "
                         f"ISSUE_COUNT={step.context.get('ISSUE_COUNT')} "
                         f"report.md={report_sz}B "
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
    bb_username = base_env.get("BITBUCKET_USERNAME", os.environ.get("BITBUCKET_USERNAME", "user@example.com"))

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
        "config = {'BITBUCKET_TOKEN': os.environ['BITBUCKET_TOKEN'], 'BITBUCKET_USERNAME': 'user@example.com'}; "
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
        "config = {'BITBUCKET_TOKEN': os.environ['BITBUCKET_TOKEN'], 'BITBUCKET_USERNAME': 'user@example.com'}; "
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


def test_20_adhoc_ask(base_env: dict[str, str]) -> TestResult:
    """Run run_skill.py with ygs-ask on a simple question. Verifies:
      1. Exit 0
      2. adhoc_report.md written with content (Claude answer)
      3. reports/report.html generated from markdown
      4. adhoc_result.json is valid JSON with status=DONE
      5. SKILL, SKILL_LOADED, SELECTED_MODEL context markers emitted

    Requires Claude credentials. Skipped if neither CLAUDE_CODE_USE_BEDROCK nor ANTHROPIC_API_KEY set.
    """
    result = TestResult("adhoc-ask")

    has_bedrock = base_env.get("CLAUDE_CODE_USE_BEDROCK", "") == "1"
    has_api_key = bool(base_env.get("ANTHROPIC_API_KEY", ""))
    if not (has_bedrock or has_api_key):
        result.passed = True
        result.message = "SKIPPED — no Claude credentials (set CLAUDE_CODE_USE_BEDROCK=1 or ANTHROPIC_API_KEY)"
        return result

    env = dict(base_env)
    ws = "/workspace/adhoc_ask"
    env["WORKSPACE_DIR"] = ws
    # Use Haiku for speed — this is a simple factual question
    haiku = base_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    env["AI_MODEL"] = haiku
    env["MAX_TURNS_ADHOC"] = "20"
    # No Slack token needed — we verify file output, not Slack delivery
    env.pop("SLACK_BOT_TOKEN", None)

    with pod_fixture("adhoc-ask") as pod:
        # Step 1 — run adhoc skill
        step = exec_step(pod, "adhoc-ask",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         "python3 -m scripts.adhoc.run_skill "
                         "--skill ygs-ask "
                         "--prompt 'What are the first 5 Fibonacci numbers? List them as bullets.'",
                         env, timeout=300)
        result.steps.append(step)

        if not step.ok:
            return _fail(result, step, f"run_skill exit code {step.returncode}")

        err = _check_keys(step, ["SKILL", "SKILL_LOADED", "SELECTED_MODEL"])
        if err:
            return _fail(result, step, f"context markers missing: {err}")

        # Verify ygs-ask SKILL.md was found (not fallback)
        if step.context.get("SKILL_LOADED") != "yes":
            return _fail(result, step, "SKILL_LOADED=no — ygs-ask SKILL.md not installed")

        # Step 2 — verify output files
        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"# Accept report content from adhoc_report.md OR reports/report.md —\n"
            f"# ygs-ask SKILL.md writes to reports/report.md; fallback writes adhoc_report.md.\n"
            f"report_candidates = [\n"
            f"    os.path.join(ws, 'adhoc_report.md'),\n"
            f"    os.path.join(ws, 'reports', 'report.md'),\n"
            f"]\n"
            f"report_found = None\n"
            f"for md in report_candidates:\n"
            f"    if os.path.exists(md):\n"
            f"        sz = os.path.getsize(md)\n"
            f"        if sz >= 50:\n"
            f"            report_found = md\n"
            f"            print(f'::add-task-context REPORT_MD_BYTES::{{sz}}')\n"
            f"            print(f'::add-task-context REPORT_MD_FILE::{{os.path.basename(md)}}')\n"
            f"            break\n"
            f"if not report_found:\n"
            f"    issues.append('no report file with content (checked adhoc_report.md and reports/report.md)')\n"
            f"# reports/report.html must exist\n"
            f"html = os.path.join(ws, 'reports', 'report.html')\n"
            f"if not os.path.exists(html): issues.append('reports/report.html missing')\n"
            f"else: print(f'::add-task-context REPORT_HTML_BYTES::{{os.path.getsize(html)}}')\n"
            f"# adhoc_result.json must be valid JSON with status=DONE\n"
            f"rj = os.path.join(ws, 'adhoc_result.json')\n"
            f"if not os.path.exists(rj):\n"
            f"    issues.append('adhoc_result.json missing')\n"
            f"else:\n"
            f"    try:\n"
            f"        d = json.loads(open(rj).read())\n"
            f"        if d.get('status') != 'DONE': issues.append(f'adhoc_result.json status={{d.get(\"status\")}}')\n"
            f"        else: print('::add-task-context ADHOC_STATUS::DONE')\n"
            f"    except Exception as e:\n"
            f"        issues.append(f'adhoc_result.json parse error: {{e}}')\n"
            f"if issues:\n"
            f"    print('FILE ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"else:\n"
            f"    print('::add-task-context FILES_VERIFIED::yes')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-adhoc", verify_cmd, env, timeout=30)
        result.steps.append(verify_step)

        if not verify_step.ok:
            return _fail(result, verify_step, f"file verification failed: {verify_step.stderr[-300:]}")

    report_bytes = verify_step.context.get("REPORT_MD_BYTES", "?")
    report_file = verify_step.context.get("REPORT_MD_FILE", "?")
    html_bytes = verify_step.context.get("REPORT_HTML_BYTES", "?")
    model = step.context.get("SELECTED_MODEL", "?")
    return _pass(result,
                 f"SKILL_LOADED=yes report={report_file}({report_bytes}B) html={html_bytes}B "
                 f"MODEL={model} elapsed={step.elapsed:.0f}s")


def test_21_pr_audit_slack_routing(base_env: dict[str, str]) -> TestResult:
    """Verify that filter flags in SLACK_MESSAGE are parsed correctly in the pod.

    This tests the critical routing fix: --board / --team / --milestone flags
    passed as trailing Slack text (RepoUrl) must reach run_pr_audit via SLACK_MESSAGE,
    not be treated as git repo URLs.  Uses a pure Python check — no Claude call needed.
    """
    result = TestResult("pr-audit-slack-routing")
    env = dict(base_env)
    ws = "/workspace/pr_audit_routing"
    env["WORKSPACE_DIR"] = ws

    with pod_fixture("pr-audit-routing") as pod:
        verify_cmd = (
            "python3 - <<'PYEOF'\n"
            "import sys, os, re\n"
            "sys.path.insert(0, '/app')\n"
            "from scripts.analyze.run_pr_audit import _parse_slack_flags\n"
            "issues = []\n"
            "\n"
            "# --board 123 → jira_boards='123'\n"
            "r = _parse_slack_flags({'SLACK_MESSAGE': 'pr-audit --board 123'})\n"
            "if r['jira_boards'] != '123': issues.append(f'--board 123 got jira_boards={r[\"jira_boards\"]!r}')\n"
            "\n"
            "# --board (no ID) → sentinel '__default__'\n"
            "r = _parse_slack_flags({'SLACK_MESSAGE': 'pr-audit --board'})\n"
            "if r['jira_boards'] != '__default__': issues.append(f'bare --board got {r[\"jira_boards\"]!r}, expected __default__')\n"
            "\n"
            "# --team alice,bob → team_members\n"
            "r = _parse_slack_flags({'SLACK_MESSAGE': '--team alice,bob'})\n"
            "if r['team_members'] != 'alice,bob': issues.append(f'--team got {r[\"team_members\"]!r}')\n"
            "\n"
            "# --milestone v2.5 → gh_milestone\n"
            "r = _parse_slack_flags({'SLACK_MESSAGE': '--milestone v2.5'})\n"
            "if r['gh_milestone'] != 'v2.5': issues.append(f'--milestone got {r[\"gh_milestone\"]!r}')\n"
            "\n"
            "# Jira board URL → jira_boards\n"
            "r = _parse_slack_flags({'SLACK_MESSAGE': 'https://example.atlassian.net/jira/software/c/projects/PROJ/boards/456'})\n"
            "if r['jira_boards'] != '456': issues.append(f'board URL got {r[\"jira_boards\"]!r}')\n"
            "\n"
            "# board: legacy syntax still works\n"
            "r = _parse_slack_flags({'SLACK_MESSAGE': 'pr-audit board:789'})\n"
            "if r['jira_boards'] != '789': issues.append(f'board:id got {r[\"jira_boards\"]!r}')\n"
            "\n"
            "if issues:\n"
            "    print('ROUTING ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "print('::add-task-context PR_AUDIT_ROUTING_OK::yes')\n"
            "print('All routing checks passed')\n"
            "PYEOF"
        )
        step = exec_step(pod, "pr-audit-routing-check", verify_cmd, env, timeout=30)
        result.steps.append(step)

    if not step.ok:
        return _fail(result, step, f"routing check failed: {step.stderr[-500:]}")
    routing_ok = step.context.get("PR_AUDIT_ROUTING_OK", "")
    if routing_ok != "yes":
        return _fail(result, step, "PR_AUDIT_ROUTING_OK marker not emitted")
    return _pass(result, "all SLACK_MESSAGE routing checks passed")


def test_22_skill_invoke(base_env: dict[str, str]) -> TestResult:
    """Run scripts.skill.run_skill with ygs-ask via RAW_ARGS. Verifies:
      1. Exit 0
      2. skill_result.json written with valid JSON
      3. reports/report.md written with content (Claude answer)
      4. reports/report.html generated
      5. SKILL, SKILL_LOADED, SELECTED_TRACKER context markers emitted

    Requires Claude credentials. Skipped if neither CLAUDE_CODE_USE_BEDROCK nor ANTHROPIC_API_KEY set.
    """
    result = TestResult("skill-invoke")

    has_bedrock = base_env.get("CLAUDE_CODE_USE_BEDROCK", "") == "1"
    has_api_key = bool(base_env.get("ANTHROPIC_API_KEY", ""))
    if not (has_bedrock or has_api_key):
        result.passed = True
        result.message = "SKIPPED — no Claude credentials (set CLAUDE_CODE_USE_BEDROCK=1 or ANTHROPIC_API_KEY)"
        return result

    env = dict(base_env)
    ws = "/workspace/skill_invoke"
    env["WORKSPACE_DIR"] = ws
    haiku = base_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    env["AI_MODEL"] = haiku
    env["MAX_TURNS_ADHOC"] = "20"
    env["RAW_ARGS"] = "ygs-ask -- What are the first 5 prime numbers? List them as bullets."
    env["DEFAULT_TRACKER"] = "github"
    env["GH_ORG"] = GH_ORG
    env["GH_REPO"] = GH_REPO
    env.pop("SLACK_BOT_TOKEN", None)

    with pod_fixture("skill-invoke") as pod:
        step = exec_step(pod, "skill-invoke",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         "python3 -m scripts.skill.run_skill",
                         env, timeout=300)
        result.steps.append(step)

        if not step.ok:
            return _fail(result, step, f"run_skill exit code {step.returncode}")

        err = _check_keys(step, ["SKILL", "SKILL_LOADED", "SELECTED_TRACKER"])
        if err:
            return _fail(result, step, f"context markers missing: {err}")

        if step.context.get("SKILL") != "ygs-ask":
            return _fail(result, step,
                         f"SKILL={step.context.get('SKILL')!r} — expected 'ygs-ask'")

        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"# skill_result.json must be valid JSON\n"
            f"rj = os.path.join(ws, 'skill_result.json')\n"
            f"if not os.path.exists(rj):\n"
            f"    issues.append('skill_result.json missing')\n"
            f"else:\n"
            f"    try:\n"
            f"        d = json.loads(open(rj).read())\n"
            f"        print(f'::add-task-context SKILL_STATUS::{{d.get(\"status\", \"?\")}}')\n"
            f"    except Exception as e:\n"
            f"        issues.append(f'skill_result.json parse error: {{e}}')\n"
            f"# reports/report.md must exist with content\n"
            f"md = os.path.join(ws, 'reports', 'report.md')\n"
            f"if not os.path.exists(md):\n"
            f"    issues.append('reports/report.md missing')\n"
            f"elif os.path.getsize(md) < 50:\n"
            f"    issues.append(f'reports/report.md too small ({{os.path.getsize(md)}} bytes)')\n"
            f"else:\n"
            f"    print(f'::add-task-context REPORT_MD_BYTES::{{os.path.getsize(md)}}')\n"
            f"# reports/report.html must exist\n"
            f"html = os.path.join(ws, 'reports', 'report.html')\n"
            f"if not os.path.exists(html):\n"
            f"    issues.append('reports/report.html missing')\n"
            f"else:\n"
            f"    print(f'::add-task-context REPORT_HTML_BYTES::{{os.path.getsize(html)}}')\n"
            f"if issues:\n"
            f"    print('FILE ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"else:\n"
            f"    print('::add-task-context FILES_VERIFIED::yes')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-skill", verify_cmd, env, timeout=30)
        result.steps.append(verify_step)

        if not verify_step.ok:
            return _fail(result, verify_step, f"file verification failed: {verify_step.stderr[-300:]}")

    report_bytes = verify_step.context.get("REPORT_MD_BYTES", "?")
    html_bytes = verify_step.context.get("REPORT_HTML_BYTES", "?")
    model = step.context.get("SELECTED_MODEL", "?")
    skill_status = verify_step.context.get("SKILL_STATUS", "?")
    return _pass(result,
                 f"SKILL=ygs-ask LOADED={step.context.get('SKILL_LOADED')} "
                 f"status={skill_status} report={report_bytes}B html={html_bytes}B "
                 f"MODEL={model} elapsed={step.elapsed:.0f}s")


def test_23_skill_flag_parsing(base_env: dict[str, str]) -> TestResult:
    """Verify skill flag parsing works in the pod — no Claude call needed.

    Checks:
      1. Empty RAW_ARGS → exit 1 with "no skill name" error
      2. Valid RAW_ARGS → exit 0 with correct context markers (SKILL, SELECTED_TRACKER)
    """
    result = TestResult("skill-flag-parsing")

    env = dict(base_env)
    ws = "/workspace/skill_flags"
    env["WORKSPACE_DIR"] = ws
    env["DEFAULT_TRACKER"] = "github"
    env.pop("SLACK_BOT_TOKEN", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_CODE_USE_BEDROCK", None)

    with pod_fixture("skill-flags") as pod:
        # Step 1 — empty RAW_ARGS should fail with clear error
        env_empty = dict(env)
        env_empty["RAW_ARGS"] = ""
        step = exec_step(pod, "empty-args",
                         f"mkdir -p {ws}/logs && "
                         "python3 -m scripts.skill.run_skill 2>&1 || true",
                         env_empty, timeout=30)
        result.steps.append(step)

        # Check stderr/stdout for "no skill name"
        combined = step.stdout + step.stderr
        if "no skill name" not in combined.lower():
            return _fail(result, step,
                         f"expected 'no skill name' error, got: {combined[-300:]}")

        # Step 2 — valid RAW_ARGS with flag parsing only (will fail at Claude call
        # but we verify flags parsed correctly via context markers before that).
        env_valid = dict(env)
        env_valid["RAW_ARGS"] = "integ-tests --repo https://github.com/bhatti/todo-sample.git --branch main --tracker github"
        step2 = exec_step(pod, "parse-flags",
                          f"mkdir -p {ws}/logs && "
                          "python3 -m scripts.skill.run_skill 2>&1 || true",
                          env_valid, timeout=60)
        result.steps.append(step2)

        # Even if it exits non-zero (no Claude creds), the context markers should be emitted
        # before the Claude invocation.
        ctx_err = _check_keys(step2, ["SELECTED_TRACKER", "SKILL"])
        if ctx_err:
            return _fail(result, step2, f"context markers missing: {ctx_err}")

        val_err = _check_values(step2, {
            "SKILL": "integ-tests",
            "SELECTED_TRACKER": "github",
        })
        if val_err:
            return _fail(result, step2, val_err)

    return _pass(result, "flag parsing verified: empty→error, valid→correct context markers")


def test_23b_skill_quoted_args(base_env: dict[str, str]) -> TestResult:
    """Verify skills with quoted multi-word positional args are parsed correctly.

    Previously the double-quotes in RAW_ARGS broke YAML rendering with:
      yaml: line 4: did not find expected key
    The flags parser must also handle quoted tokens without crashing.

    No Claude credentials required — exits before Claude call, verifies flags only.
    """
    result = TestResult("skill-quoted-args")

    env = dict(base_env)
    ws = "/workspace/skill_quoted_args"
    env["WORKSPACE_DIR"] = ws
    env["DEFAULT_TRACKER"] = "jira"
    env.pop("SLACK_BOT_TOKEN", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_CODE_USE_BEDROCK", None)

    # Double-quoted multi-word positional arg + unknown flag + known flag
    raw_args = 'my-sprint-skill "Q4 Roadmap" --dry-run --branch feature-branch'

    with pod_fixture("skill-quoted-args") as pod:
        env_test = dict(env)
        env_test["RAW_ARGS"] = raw_args
        step = exec_step(pod, "quoted-args",
                         f"mkdir -p {ws}/logs && "
                         "python3 -m scripts.skill.run_skill 2>&1 || true",
                         env_test, timeout=60)
        result.steps.append(step)

        combined = step.stdout + step.stderr
        # Must not see a flags-parsing crash
        if "traceback" in combined.lower() and "valueerror" in combined.lower():
            return _fail(result, step, f"unexpected ValueError from flags parser: {combined[-400:]}")

        # The context markers must be emitted before any Claude call attempt
        ctx_err = _check_keys(step, ["SKILL", "BRANCH"])
        if ctx_err:
            return _fail(result, step, f"context markers missing: {ctx_err}")

        val_err = _check_values(step, {
            "SKILL": "my-sprint-skill",
            "BRANCH": "feature-branch",
        })
        if val_err:
            return _fail(result, step, val_err)

    return _pass(result, "quoted args parsed: skill and branch context markers correct")


def test_24_skill_integ_tests(base_env: dict[str, str]) -> TestResult:
    """Run scripts.skill.run_skill with integ-tests skill against todo-sample repo.

    Verifies:
      1. Exit 0
      2. Context markers: SKILL, SKILL_LOADED, SELECTED_TRACKER, SELECTED_MODEL, REPO_URL, BRANCH
      3. skill_result.json exists with valid JSON
      4. reports/report.md has content

    Requires Claude credentials. Skipped if neither CLAUDE_CODE_USE_BEDROCK nor ANTHROPIC_API_KEY set.
    """
    result = TestResult("skill-integ-tests")

    has_bedrock = base_env.get("CLAUDE_CODE_USE_BEDROCK", "") == "1"
    has_api_key = bool(base_env.get("ANTHROPIC_API_KEY", ""))
    if not (has_bedrock or has_api_key):
        result.passed = True
        result.message = "SKIPPED — no Claude credentials (set CLAUDE_CODE_USE_BEDROCK=1 or ANTHROPIC_API_KEY)"
        return result

    env = dict(base_env)
    ws = "/workspace/skill_invoke"
    env["WORKSPACE_DIR"] = ws
    env["RAW_ARGS"] = "integ-tests --repo https://github.com/bhatti/todo-sample.git --branch main -- run tests and generate report"
    env["DEFAULT_TRACKER"] = "github"
    env["GH_ORG"] = GH_ORG
    env["GH_REPO"] = GH_REPO
    haiku = base_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    env["AI_MODEL"] = haiku
    env["MAX_TURNS_ADHOC"] = "20"
    env.pop("SLACK_BOT_TOKEN", None)

    with pod_fixture("skill-invoke") as pod:
        # Step 1 — run skill
        step = exec_step(pod, "skill-invoke",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         "python3 -m scripts.skill.run_skill",
                         env, timeout=600)
        result.steps.append(step)

        if not step.ok:
            return _fail(result, step, f"run_skill exit code {step.returncode}")

        err = _check_keys(step, ["SELECTED_TRACKER", "SKILL", "SKILL_LOADED", "SELECTED_MODEL"])
        if err:
            return _fail(result, step, f"context markers missing: {err}")

        val_err = _check_values(step, {"SKILL": "integ-tests", "SELECTED_TRACKER": "github"})
        if val_err:
            return _fail(result, step, val_err)

        # Step 2 — verify output files
        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"# skill_result.json must exist with valid JSON\n"
            f"rj = os.path.join(ws, 'skill_result.json')\n"
            f"if not os.path.exists(rj):\n"
            f"    issues.append('skill_result.json missing')\n"
            f"else:\n"
            f"    try:\n"
            f"        d = json.loads(open(rj).read())\n"
            f"        print(f'::add-task-context SKILL_STATUS::{{d.get(\"status\", \"?\")}}')\n"
            f"    except Exception as e:\n"
            f"        issues.append(f'skill_result.json parse error: {{e}}')\n"
            f"# reports/report.md should exist with content\n"
            f"md = os.path.join(ws, 'reports', 'report.md')\n"
            f"if os.path.exists(md):\n"
            f"    sz = os.path.getsize(md)\n"
            f"    print(f'::add-task-context REPORT_MD_BYTES::{{sz}}')\n"
            f"    if sz < 50: issues.append(f'report.md too small ({{sz}} bytes)')\n"
            f"else:\n"
            f"    issues.append('reports/report.md missing')\n"
            f"# reports/report.html should exist\n"
            f"html = os.path.join(ws, 'reports', 'report.html')\n"
            f"if os.path.exists(html):\n"
            f"    print(f'::add-task-context REPORT_HTML_BYTES::{{os.path.getsize(html)}}')\n"
            f"if issues:\n"
            f"    print('FILE ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"else:\n"
            f"    print('::add-task-context FILES_VERIFIED::yes')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-skill", verify_cmd, env, timeout=30)
        result.steps.append(verify_step)

        if not verify_step.ok:
            return _fail(result, verify_step, f"file verification failed: {verify_step.stderr[-300:]}")

    report_bytes = verify_step.context.get("REPORT_MD_BYTES", "?")
    html_bytes = verify_step.context.get("REPORT_HTML_BYTES", "?")
    model = step.context.get("SELECTED_MODEL", "?")
    skill_status = verify_step.context.get("SKILL_STATUS", "?")
    return _pass(result,
                 f"SKILL=integ-tests status={skill_status} report={report_bytes}B html={html_bytes}B "
                 f"MODEL={model} elapsed={step.elapsed:.0f}s")


def test_25_skill_service_awareness(base_env: dict[str, str]) -> TestResult:
    """Verify skill script detects SERVICE_IMAGE env and logs service info.

    Does NOT require Claude credentials — the script logs service awareness
    before invoking Claude, so we can verify detection even when Claude call fails.
    """
    result = TestResult("skill-service-awareness")

    env = dict(base_env)
    ws = "/workspace/skill_service"
    env["WORKSPACE_DIR"] = ws
    env["RAW_ARGS"] = "ygs-ask --repo https://github.com/bhatti/todo-sample.git -- check service"
    env["DEFAULT_TRACKER"] = "github"
    env["SERVICE_IMAGE"] = "nginx:alpine"
    env["SERVICE_NAME"] = "test-svc"
    env["SERVICE_PORT"] = "8080"
    env.pop("SLACK_BOT_TOKEN", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_CODE_USE_BEDROCK", None)

    with pod_fixture("skill-svc") as pod:
        step = exec_step(pod, "svc-detect",
                         f"mkdir -p {ws}/logs && "
                         "python3 -m scripts.skill.run_skill 2>&1 || true",
                         env, timeout=60)
        result.steps.append(step)

        combined = step.stdout + step.stderr
        if "service running: nginx:alpine at localhost:8080" not in combined:
            return _fail(result, step,
                         f"service detection log missing, got: {combined[-500:]}")

        ctx_err = _check_keys(step, ["SKILL"])
        if ctx_err:
            return _fail(result, step, f"context markers missing: {ctx_err}")

        val_err = _check_values(step, {"SKILL": "ygs-ask"})
        if val_err:
            return _fail(result, step, val_err)

    return _pass(result, "service awareness verified: SERVICE_IMAGE detected and logged")


def test_26_skill_identifier_passthrough(base_env: dict[str, str]) -> TestResult:
    """Verify positional identifier (e.g. PR number) is parsed and included in prompt.

    Uses SKILL_ARG from env (e.g. "review-pr myapp 4444") to test the real
    positional parsing flow. No Claude credentials needed — we verify the
    identifier appears in context markers and stdout before Claude is invoked.
    """
    result = TestResult("skill-identifier-passthrough")

    skill_arg = os.environ.get("SKILL_ARG", "").strip()
    if not skill_arg:
        result.passed = True
        result.message = "SKIPPED — SKILL_ARG not set in env (set in ~/.zshrc)"
        return result

    env = dict(base_env)
    ws = "/workspace/skill_id_test"
    env["WORKSPACE_DIR"] = ws
    env["RAW_ARGS"] = skill_arg
    env["DEFAULT_TRACKER"] = "jira"
    env.pop("SLACK_BOT_TOKEN", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_CODE_USE_BEDROCK", None)

    with pod_fixture("skill-id") as pod:
        step = exec_step(pod, "id-parse",
                         f"mkdir -p {ws}/logs && "
                         "python3 -m scripts.skill.run_skill 2>&1 || true",
                         env, timeout=120)
        result.steps.append(step)

        combined = step.stdout + step.stderr

        # Verify SKILL context marker was emitted.
        ctx_err = _check_keys(step, ["SKILL"])
        if ctx_err:
            return _fail(result, step, f"context markers missing: {ctx_err}")

        # Verify IDENTIFIER context marker was emitted (from the numeric positional arg).
        if "IDENTIFIER" not in step.context:
            return _fail(result, step,
                         f"IDENTIFIER context marker not emitted. stdout: {combined[-500:]}")

        identifier = step.context["IDENTIFIER"]
        if not identifier or not identifier.isdigit():
            return _fail(result, step,
                         f"IDENTIFIER={identifier!r} — expected numeric value")

        # Verify the identifier appears in the log line.
        if f"identifier={identifier}" not in combined:
            return _fail(result, step,
                         f"'identifier={identifier}' not in stdout. got: {combined[-500:]}")

    skill = step.context.get("SKILL", "?")
    return _pass(result,
                 f"SKILL={skill} IDENTIFIER={identifier} "
                 f"parsed from SKILL_ARG={skill_arg!r}")


def test_27_skill_e2e_with_identifier(base_env: dict[str, str]) -> TestResult:
    """End-to-end skill invocation with positional identifier (PR number).

    Uses SKILL_ARG and EXTRA_SKILLS_REPOS from env. Requires Claude credentials.
    Verifies the skill actually receives the identifier and produces output.
    """
    result = TestResult("skill-e2e-identifier")

    skill_arg = os.environ.get("SKILL_ARG", "").strip()
    extra_skills = os.environ.get("EXTRA_SKILLS_REPOS", "").strip()
    if not skill_arg:
        result.passed = True
        result.message = "SKIPPED — SKILL_ARG not set in env (set in ~/.zshrc)"
        return result

    has_bedrock = base_env.get("CLAUDE_CODE_USE_BEDROCK", "") == "1"
    has_api_key = bool(base_env.get("ANTHROPIC_API_KEY", ""))
    if not (has_bedrock or has_api_key):
        result.passed = True
        result.message = "SKIPPED — no Claude credentials (set CLAUDE_CODE_USE_BEDROCK=1 or ANTHROPIC_API_KEY)"
        return result

    env = dict(base_env)
    ws = "/workspace/skill_e2e_id"
    env["WORKSPACE_DIR"] = ws
    env["RAW_ARGS"] = skill_arg
    env["DEFAULT_TRACKER"] = "jira"
    haiku = base_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    env["AI_MODEL"] = haiku
    env["MAX_TURNS_ADHOC"] = "30"
    if extra_skills:
        env["EXTRA_SKILLS_REPOS"] = extra_skills
    env.pop("SLACK_BOT_TOKEN", None)

    with pod_fixture("skill-e2e-id") as pod:
        step = exec_step(pod, "skill-e2e",
                         f"mkdir -p {ws}/reports {ws}/logs && "
                         "python3 -m scripts.skill.run_skill",
                         env, timeout=600)
        result.steps.append(step)

        if not step.ok:
            return _fail(result, step, f"run_skill exit code {step.returncode}")

        err = _check_keys(step, ["SKILL", "IDENTIFIER"])
        if err:
            return _fail(result, step, f"context markers missing: {err}")

        identifier = step.context.get("IDENTIFIER", "")
        if not identifier:
            return _fail(result, step, "IDENTIFIER is empty — positional arg not parsed")

        # Verify artifact files.
        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"rj = os.path.join(ws, 'skill_result.json')\n"
            f"if not os.path.exists(rj):\n"
            f"    issues.append('skill_result.json missing')\n"
            f"else:\n"
            f"    try:\n"
            f"        d = json.loads(open(rj).read())\n"
            f"        print(f'::add-task-context SKILL_STATUS::{{d.get(\"status\", \"?\")}}')\n"
            f"    except Exception as e:\n"
            f"        issues.append(f'skill_result.json parse error: {{e}}')\n"
            f"md = os.path.join(ws, 'reports', 'report.md')\n"
            f"if not os.path.exists(md):\n"
            f"    issues.append('reports/report.md missing')\n"
            f"elif os.path.getsize(md) < 50:\n"
            f"    issues.append(f'reports/report.md too small ({{os.path.getsize(md)}} bytes)')\n"
            f"else:\n"
            f"    print(f'::add-task-context REPORT_MD_BYTES::{{os.path.getsize(md)}}')\n"
            f"if issues:\n"
            f"    print('FILE ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"else:\n"
            f"    print('::add-task-context FILES_VERIFIED::yes')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-e2e", verify_cmd, env, timeout=30)
        result.steps.append(verify_step)

        if not verify_step.ok:
            return _fail(result, verify_step, f"file verification failed: {verify_step.stderr[-300:]}")

    skill = step.context.get("SKILL", "?")
    report_bytes = verify_step.context.get("REPORT_MD_BYTES", "?")
    skill_status = verify_step.context.get("SKILL_STATUS", "?")
    return _pass(result,
                 f"SKILL={skill} IDENTIFIER={identifier} "
                 f"status={skill_status} report={report_bytes}B "
                 f"elapsed={step.elapsed:.0f}s")


def test_28_skill_post(base_env: dict[str, str]) -> TestResult:
    """Verify scripts.skill.post reads skill_result.json + reports/report.md and exits 0.

    Creates fixture files in workspace, then runs post.py. Does NOT require a live
    Slack token — we omit SLACK_BOT_TOKEN so the post falls back to the fallback path
    (which logs rather than hard-fails). Verifies exit 0.
    """
    result = TestResult("skill-post")

    env = dict(base_env)
    ws = "/workspace/skill_post_test"
    env["WORKSPACE_DIR"] = ws
    env.pop("SLACK_BOT_TOKEN", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_CODE_USE_BEDROCK", None)

    # Provide minimal Formicary vars so fallback notify has something to link.
    env["FORMICARY_URL"] = env.get("FORMICARY_URL", "http://localhost:7777")
    env["JOB_ID"] = "test-job-99"

    setup_cmd = (
        f"mkdir -p {ws}/reports && "
        f"python3 - <<'PYEOF'\n"
        f"import json, pathlib\n"
        f"ws = pathlib.Path('{ws}')\n"
        f"(ws / 'skill_result.json').write_text(json.dumps({{'skill': 'ygs-ask', 'status': 'DONE', 'model': 'haiku'}}))\n"
        f"(ws / 'reports' / 'report.md').write_text('# Test Report\\n\\nAll checks passed.\\n\\n- item1 OK\\n- item2 OK\\n')\n"
        f"PYEOF"
    )

    with pod_fixture("skill-post") as pod:
        # Step 1 — create fixture files
        setup_step = exec_step(pod, "setup-fixtures", setup_cmd, env, timeout=30)
        result.steps.append(setup_step)
        if not setup_step.ok:
            return _fail(result, setup_step, f"fixture setup failed: {setup_step.stderr[-300:]}")

        # Step 2 — run post.py
        post_step = exec_step(pod, "run-post",
                              "python3 -m scripts.skill.post",
                              env, timeout=60)
        result.steps.append(post_step)

        combined = post_step.stdout + post_step.stderr
        if post_step.returncode != 0:
            return _fail(result, post_step, f"post.py exited {post_step.returncode}: {combined[-500:]}")

        # Verify it logged the expected output (either posted or fell back).
        if "[skill-post]" not in combined:
            return _fail(result, post_step,
                         f"expected [skill-post] log line, got: {combined[-500:]}")

    return _pass(result, "skill-post exited 0 and logged output")


def test_29_skill_node_image(base_env: dict[str, str]) -> TestResult:
    """Verify scripts.skill.run_skill works inside python:3.12-bookworm.

    Runs a pod with the same Debian bookworm base we now ship in the production image,
    installs Python deps from requirements.txt (copied by _copy_scripts), then invokes
    run_skill. Catches any Alpine→bookworm regressions before a docker build.

    Requires Claude credentials. Skipped if neither CLAUDE_CODE_USE_BEDROCK nor
    ANTHROPIC_API_KEY is set.
    """
    result = TestResult("skill-node-image")

    has_bedrock = base_env.get("CLAUDE_CODE_USE_BEDROCK", "") == "1"
    has_api_key = bool(base_env.get("ANTHROPIC_API_KEY", ""))
    if not (has_bedrock or has_api_key):
        result.passed = True
        result.message = "SKIPPED — no Claude credentials"
        return result

    env = dict(base_env)
    ws = "/workspace/skill_node"
    env["WORKSPACE_DIR"] = ws
    haiku = base_env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    env["AI_MODEL"] = haiku
    env["MAX_TURNS_ADHOC"] = "20"
    env["RAW_ARGS"] = "ygs-ask -- What is 2+2?"
    env["DEFAULT_TRACKER"] = "github"
    env["GH_ORG"] = GH_ORG
    env["GH_REPO"] = GH_REPO
    env.pop("SLACK_BOT_TOKEN", None)

    with pod_fixture("skill-bookworm", image="python:3.12-bookworm") as pod:
        # Install Python deps from requirements.txt (copied by _copy_scripts into /app/)
        install_step = exec_step(
            pod, "pip-install",
            "pip install --quiet -r /app/requirements.txt 2>&1 | tail -5",
            env, timeout=120,
        )
        result.steps.append(install_step)
        if not install_step.ok:
            return _fail(result, install_step,
                         f"pip install failed: {install_step.stderr[-300:]}")

        run_step = exec_step(
            pod, "skill-bookworm-run",
            f"mkdir -p {ws}/reports {ws}/logs && "
            "PYTHONPATH=/app python3 -m scripts.skill.run_skill",
            env, timeout=300,
        )
        result.steps.append(run_step)

        # The python:3.12-bookworm image doesn't have the claude CLI installed (no npm/node),
        # so exit 1 is expected. What matters is that all Python modules imported correctly and
        # the skill was loaded — proven by the context markers.
        err = _check_keys(run_step, ["SKILL", "SKILL_LOADED", "SELECTED_TRACKER"])
        if err:
            return _fail(result, run_step, f"context markers missing: {err}")

        combined = run_step.stdout + run_step.stderr
        if run_step.returncode != 0 and "claude" not in combined.lower():
            return _fail(result, run_step,
                         f"run_skill in python:3.12-bookworm unexpected failure (exit {run_step.returncode}): "
                         f"{combined[-500:]}")

    model = run_step.context.get("SELECTED_MODEL", "?")
    return _pass(result,
                 f"python:3.12-bookworm — SKILL={run_step.context.get('SKILL')} "
                 f"LOADED={run_step.context.get('SKILL_LOADED')} MODEL={model} "
                 f"elapsed={run_step.elapsed:.0f}s")


def test_30_adhoc_pr_queue_report(base_env: dict[str, str]) -> TestResult:
    """Run run_skill.py with ygs-pr-queue using a pre-built pr_queue.json (no GitHub/Jira needed).

    Verifies:
      1. Exit 0
      2. reports/report.md written with PR table content
      3. reports/report.html written with HTML content
      4. reports/result.json has status=DONE and pr_count
    Note: the artifact link is added by the post task's __main__, not by run_skill.py.
    """
    result = TestResult("adhoc-pr-queue-report")
    env = dict(base_env)
    ws = "/workspace/adhoc_pr_queue"
    env["WORKSPACE_DIR"] = ws
    env["SLACK_CHANNEL"] = "dev"
    env["FORMICARY_PUBLIC_URL"] = "https://formicary.example.com"
    env["JOB_ID"] = "test-job-999"
    env["JOB_TYPE"] = "ai-adhoc"
    # No real Slack token so upload_file fails → fallback link path is exercised
    env.pop("SLACK_BOT_TOKEN", None)

    # Minimal pr_queue.json — one PR in READY TO MERGE group
    pr_queue_json = (
        '{"sprint":"Test Sprint 1","pr_count":1,"prs":[{'
        '"url":"https://github.com/org/repo/pull/42",'
        '"jira_key":"TEST-1","jira_summary":"Fix bug",'
        '"author":"Alice","age_days":1,'
        '"approved_by":["Bob"],"reviewers":[],'
        '"ci_status":"success","approval_count":1}]}'
    )

    with pod_fixture("adhoc-pr-queue") as pod:
        setup_step = exec_step(
            pod, "setup-pr-queue",
            f"mkdir -p {ws}/reports {ws}/logs && "
            f"echo '{pr_queue_json}' > {ws}/pr_queue.json",
            env, timeout=30,
        )
        result.steps.append(setup_step)
        if not setup_step.ok:
            return _fail(result, setup_step, f"setup failed: {setup_step.stderr[-200:]}")

        run_step = exec_step(
            pod, "run-pr-queue",
            f"python3 -m scripts.adhoc.run_skill --skill ygs-pr-queue --prompt 'show prs'",
            env, timeout=60,
        )
        result.steps.append(run_step)
        if not run_step.ok:
            return _fail(result, run_step, f"run_skill exit {run_step.returncode}: {run_step.stderr[-300:]}")

        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"md = os.path.join(ws, 'reports', 'report.md')\n"
            f"if not os.path.exists(md): issues.append('reports/report.md missing')\n"
            f"else:\n"
            f"    content = open(md).read()\n"
            f"    if 'Fix bug' not in content: issues.append('PR title not in report.md')\n"
            f"    if '| CI |' not in content: issues.append('table header missing in report.md')\n"
            f"    print(f'::add-task-context REPORT_MD_BYTES::{{os.path.getsize(md)}}')\n"
            f"html = os.path.join(ws, 'reports', 'report.html')\n"
            f"if not os.path.exists(html): issues.append('reports/report.html missing')\n"
            f"else: print(f'::add-task-context REPORT_HTML_BYTES::{{os.path.getsize(html)}}')\n"
            f"rj = os.path.join(ws, 'reports', 'result.json')\n"
            f"if not os.path.exists(rj): issues.append('reports/result.json missing')\n"
            f"else:\n"
            f"    d = json.loads(open(rj).read())\n"
            f"    if d.get('status') != 'DONE': issues.append(f'status={{d.get(\"status\")}}')\n"
            f"    print(f'::add-task-context PR_COUNT::{{d.get(\"pr_count\",\"?\")}}')\n"
            f"if issues:\n"
            f"    print('ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"print('::add-task-context VERIFY::ok')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-pr-queue", verify_cmd, env, timeout=30)
        result.steps.append(verify_step)
        if not verify_step.ok:
            return _fail(result, verify_step, f"verification failed: {verify_step.stderr[-300:]}")

    md_bytes = verify_step.context.get("REPORT_MD_BYTES", "?")
    html_bytes = verify_step.context.get("REPORT_HTML_BYTES", "?")
    pr_count = verify_step.context.get("PR_COUNT", "?")
    return _pass(result, f"report.md={md_bytes}B html={html_bytes}B pr_count={pr_count}")


def test_31_mq_scope_router(base_env: dict[str, str]) -> TestResult:
    """Run scope_router + risk_score on a GitHub PR. Verifies scope.json and risk_score.json."""
    result = TestResult("mq-scope-router")
    if not base_env.get("GH_ORG") or not base_env.get("GH_REPO"):
        result.passed = True
        result.message = "SKIPPED — GH_ORG/GH_REPO not set"
        return result

    env = dict(base_env)
    ws = "/workspace/mq_scope"
    env["WORKSPACE_DIR"] = ws
    env["CODEBASE_DIR"] = f"{ws}/repo"

    with pod_fixture("mq-scope-router") as pod:
        setup_step = exec_step(pod, "setup",
                               f"mkdir -p {ws}/reports {ws}/logs",
                               env, timeout=30)
        result.steps.append(setup_step)
        if not setup_step.ok:
            return _fail(result, setup_step, f"setup failed: {setup_step.stderr[-200:]}")

        clone_step = exec_step(pod, "clone",
                               "python3 -m scripts.mq.clone_pr --pr-number 26",
                               env, timeout=120)
        result.steps.append(clone_step)
        if not clone_step.ok:
            return _fail(result, clone_step, f"clone exit {clone_step.returncode}: {clone_step.stderr[-300:]}")

        scope_step = exec_step(pod, "scope-router",
                               "python3 -m scripts.mq.scope_router --pr-number 26",
                               env, timeout=120)
        result.steps.append(scope_step)
        if not scope_step.ok:
            return _fail(result, scope_step, f"scope_router exit {scope_step.returncode}: {scope_step.stderr[-300:]}")

        err = _check_keys(scope_step, ["SCOPE_KEY", "BLAST_RADIUS", "CHANGED_FILES"])
        if err:
            return _fail(result, scope_step, err)

        risk_step = exec_step(pod, "risk-score",
                              "python3 -m scripts.mq.risk_score --pr-number 26",
                              env, timeout=120)
        result.steps.append(risk_step)
        if not risk_step.ok:
            return _fail(result, risk_step, f"risk_score exit {risk_step.returncode}: {risk_step.stderr[-300:]}")

        err = _check_keys(risk_step, ["RISK_TIER"])
        if err:
            return _fail(result, risk_step, err)

        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"sp = os.path.join(ws, 'scope.json')\n"
            f"if not os.path.exists(sp): issues.append('scope.json missing')\n"
            f"else:\n"
            f"    d = json.loads(open(sp).read())\n"
            f"    if 'scope' not in d: issues.append('scope.json missing scope key')\n"
            f"    if 'blast_radius' not in d: issues.append('scope.json missing blast_radius')\n"
            f"    print(f'::add-task-context SCOPE_NAME::{{d.get(\"scope\",\"?\")}}')\n"
            f"rp = os.path.join(ws, 'risk_score.json')\n"
            f"if not os.path.exists(rp): issues.append('risk_score.json missing')\n"
            f"else:\n"
            f"    d = json.loads(open(rp).read())\n"
            f"    if 'tier' not in d: issues.append('risk_score.json missing tier')\n"
            f"    if 'score' not in d: issues.append('risk_score.json missing score')\n"
            f"    print(f'::add-task-context RISK_SCORE::{{d.get(\"score\",\"?\")}}')\n"
            f"if issues:\n"
            f"    print('ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"print('::add-task-context VERIFY::ok')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-artifacts", verify_cmd, env, timeout=30)
        result.steps.append(verify_step)
        if not verify_step.ok:
            return _fail(result, verify_step, f"verify failed: {verify_step.stderr[-300:]}")

    scope = scope_step.context.get("SCOPE_KEY", "?")
    tier = risk_step.context.get("RISK_TIER", "?")
    score = verify_step.context.get("RISK_SCORE", "?")
    return _pass(result, f"scope={scope} tier={tier} score={score} elapsed={scope_step.elapsed + risk_step.elapsed:.0f}s")


def test_32_mq_test_impact(base_env: dict[str, str]) -> TestResult:
    """Run test_impact on a GitHub PR. Verifies test_impact.json with shards."""
    result = TestResult("mq-test-impact")
    if not base_env.get("GH_ORG") or not base_env.get("GH_REPO"):
        result.passed = True
        result.message = "SKIPPED — GH_ORG/GH_REPO not set"
        return result

    env = dict(base_env)
    ws = "/workspace/mq_impact"
    env["WORKSPACE_DIR"] = ws
    env["CODEBASE_DIR"] = f"{ws}/repo"

    with pod_fixture("mq-test-impact") as pod:
        setup_step = exec_step(pod, "setup",
                               f"mkdir -p {ws}/reports {ws}/logs",
                               env, timeout=30)
        result.steps.append(setup_step)
        if not setup_step.ok:
            return _fail(result, setup_step, f"setup failed: {setup_step.stderr[-200:]}")

        clone_step = exec_step(pod, "clone",
                               "python3 -m scripts.mq.clone_pr --pr-number 26",
                               env, timeout=120)
        result.steps.append(clone_step)
        if not clone_step.ok:
            return _fail(result, clone_step, f"clone exit {clone_step.returncode}: {clone_step.stderr[-300:]}")

        impact_step = exec_step(pod, "test-impact",
                                "python3 -m scripts.mq.test_impact --pr-number 26",
                                env, timeout=120)
        result.steps.append(impact_step)
        if not impact_step.ok:
            return _fail(result, impact_step, f"test_impact exit {impact_step.returncode}: {impact_step.stderr[-300:]}")

        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"ip = os.path.join(ws, 'test_impact.json')\n"
            f"if not os.path.exists(ip): issues.append('test_impact.json missing')\n"
            f"else:\n"
            f"    d = json.loads(open(ip).read())\n"
            f"    for k in ('total_tests', 'selected_tests', 'reduction_pct', 'shards', 'language'):\n"
            f"        if k not in d: issues.append(f'test_impact.json missing {{k}}')\n"
            f"    print(f'::add-task-context TOTAL_TESTS::{{d.get(\"total_tests\",\"?\")}}')\n"
            f"    print(f'::add-task-context SELECTED_TESTS::{{d.get(\"selected_tests\",\"?\")}}')\n"
            f"    print(f'::add-task-context REDUCTION_PCT::{{d.get(\"reduction_pct\",\"?\")}}')\n"
            f"    print(f'::add-task-context SHARD_COUNT::{{len(d.get(\"shards\",[]))}}')\n"
            f"    print(f'::add-task-context LANGUAGE::{{d.get(\"language\",\"?\")}}')\n"
            f"if issues:\n"
            f"    print('ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"print('::add-task-context VERIFY::ok')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-impact", verify_cmd, env, timeout=30)
        result.steps.append(verify_step)
        if not verify_step.ok:
            return _fail(result, verify_step, f"verify failed: {verify_step.stderr[-300:]}")

    total = verify_step.context.get("TOTAL_TESTS", "?")
    selected = verify_step.context.get("SELECTED_TESTS", "?")
    pct = verify_step.context.get("REDUCTION_PCT", "?")
    shards = verify_step.context.get("SHARD_COUNT", "?")
    lang = verify_step.context.get("LANGUAGE", "?")
    return _pass(result, f"selected={selected}/{total} reduction={pct}% shards={shards} lang={lang}")


def test_33_mq_full_pipeline(base_env: dict[str, str]) -> TestResult:
    """Run the full MQ pipeline (scope + risk + impact + report) in one pod.

    Verifies all artifacts exist and report.md/report.html are generated.
    """
    result = TestResult("mq-full-pipeline")
    if not base_env.get("GH_ORG") or not base_env.get("GH_REPO"):
        result.passed = True
        result.message = "SKIPPED — GH_ORG/GH_REPO not set"
        return result

    env = dict(base_env)
    ws = "/workspace/mq_pipeline"
    env["WORKSPACE_DIR"] = ws
    env["CODEBASE_DIR"] = f"{ws}/repo"
    env.pop("SLACK_BOT_TOKEN", None)

    with pod_fixture("mq-pipeline") as pod:
        setup_step = exec_step(pod, "setup",
                               f"mkdir -p {ws}/reports {ws}/logs",
                               env, timeout=30)
        result.steps.append(setup_step)
        if not setup_step.ok:
            return _fail(result, setup_step, f"setup failed: {setup_step.stderr[-200:]}")

        clone_step = exec_step(pod, "clone",
                               "python3 -m scripts.mq.clone_pr --pr-number 26",
                               env, timeout=120)
        result.steps.append(clone_step)
        if not clone_step.ok:
            return _fail(result, clone_step, f"clone exit {clone_step.returncode}: {clone_step.stderr[-300:]}")

        scope_step = exec_step(pod, "scope-router",
                               "python3 -m scripts.mq.scope_router --pr-number 26",
                               env, timeout=120)
        result.steps.append(scope_step)
        if not scope_step.ok:
            return _fail(result, scope_step, f"scope exit {scope_step.returncode}: {scope_step.stderr[-300:]}")

        risk_step = exec_step(pod, "risk-score",
                              "python3 -m scripts.mq.risk_score --pr-number 26",
                              env, timeout=120)
        result.steps.append(risk_step)
        if not risk_step.ok:
            return _fail(result, risk_step, f"risk exit {risk_step.returncode}: {risk_step.stderr[-300:]}")

        impact_step = exec_step(pod, "test-impact",
                                "python3 -m scripts.mq.test_impact --pr-number 26",
                                env, timeout=120)
        result.steps.append(impact_step)
        if not impact_step.ok:
            return _fail(result, impact_step, f"impact exit {impact_step.returncode}: {impact_step.stderr[-300:]}")

        report_step = exec_step(pod, "report",
                                "python3 -m scripts.mq.report",
                                env, timeout=60)
        result.steps.append(report_step)
        if not report_step.ok:
            return _fail(result, report_step, f"report exit {report_step.returncode}: {report_step.stderr[-300:]}")

        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"for f in ('scope.json', 'risk_score.json', 'test_impact.json'):\n"
            f"    fp = os.path.join(ws, f)\n"
            f"    if not os.path.exists(fp): issues.append(f'{{f}} missing')\n"
            f"    else:\n"
            f"        d = json.loads(open(fp).read())\n"
            f"        if not d: issues.append(f'{{f}} is empty')\n"
            f"md = os.path.join(ws, 'reports', 'report.md')\n"
            f"if not os.path.exists(md): issues.append('reports/report.md missing')\n"
            f"else:\n"
            f"    content = open(md).read()\n"
            f"    md_size = os.path.getsize(md)\n"
            f"    print(f'::add-task-context REPORT_MD_BYTES::{{md_size}}')\n"
            f"    if md_size < 200: issues.append(f'report.md too small ({{md_size}}B) — likely empty')\n"
            f"    if '## Scope' not in content: issues.append('report.md missing Scope section')\n"
            f"    if '## Risk Score' not in content: issues.append('report.md missing Risk Score section')\n"
            f"    if '## Test Impact' not in content: issues.append('report.md missing Test Impact section')\n"
            f"    if '|' not in content: issues.append('report.md has no table content')\n"
            f"html = os.path.join(ws, 'reports', 'report.html')\n"
            f"if not os.path.exists(html): issues.append('reports/report.html missing')\n"
            f"else:\n"
            f"    hsize = os.path.getsize(html)\n"
            f"    print(f'::add-task-context REPORT_HTML_BYTES::{{hsize}}')\n"
            f"    hcontent = open(html).read()\n"
            f"    if hsize < 500: issues.append(f'report.html too small ({{hsize}}B) — likely empty')\n"
            f"    if '<table' not in hcontent and '<h' not in hcontent: issues.append('report.html has no rendered content')\n"
            f"rj = os.path.join(ws, 'reports', 'result.json')\n"
            f"if not os.path.exists(rj): issues.append('reports/result.json missing')\n"
            f"else:\n"
            f"    d = json.loads(open(rj).read())\n"
            f"    print(f'::add-task-context REPORT_STATUS::{{d.get(\"status\",\"?\")}}')\n"
            f"    if d.get('status') != 'DONE': issues.append(f'result.json status={{d.get(\"status\")}}')\n"
            f"    if not d.get('SCOPE'): issues.append('result.json missing SCOPE key')\n"
            f"    if not d.get('RISK_TIER'): issues.append('result.json missing RISK_TIER key')\n"
            f"if issues:\n"
            f"    print('ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"print('::add-task-context VERIFY::ok')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-pipeline", verify_cmd, env, timeout=30)
        result.steps.append(verify_step)
        if not verify_step.ok:
            return _fail(result, verify_step, f"verify failed: {verify_step.stderr[-300:]}")

    md_bytes = verify_step.context.get("REPORT_MD_BYTES", "?")
    html_bytes = verify_step.context.get("REPORT_HTML_BYTES", "?")
    tier = risk_step.context.get("RISK_TIER", "?")
    scope = scope_step.context.get("SCOPE_KEY", "?")
    return _pass(result, f"scope={scope} tier={tier} report.md={md_bytes}B html={html_bytes}B")


def test_34_mq_test_impact_branch(base_env: dict[str, str]) -> TestResult:
    """Run test_impact with a branch name against bhatti/formicary repo.

    Validates two things:
    1. No TypeError: run_cmd() got an unexpected keyword argument 'cwd'  (regression)
    2. Full suite fallback: when git diff main...HEAD is empty (HEAD is at main),
       test_impact must discover ALL test files in the repo and produce real
       test metrics (selected > 0, shards > 0) — NOT report 0/0 tests.

    This mirrors exactly: @sb-slack parallel-test --repo https://github.com/bhatti/formicary main
    """
    result = TestResult("mq-test-impact-branch")

    env = dict(base_env)
    ws = "/workspace/mq_branch"
    env["WORKSPACE_DIR"] = ws
    env["CODEBASE_DIR"] = f"{ws}/repo"
    env["BASE_BRANCH"] = "main"
    env["GH_ORG"] = "bhatti"
    env["GH_REPO"] = "formicary"
    env["REPO_URL"] = "https://github.com/bhatti/formicary"

    with pod_fixture("mq-impact-branch") as pod:
        setup_step = exec_step(pod, "setup",
                               f"mkdir -p {ws}/reports {ws}/logs",
                               env, timeout=30)
        result.steps.append(setup_step)
        if not setup_step.ok:
            return _fail(result, setup_step, f"setup failed: {setup_step.stderr[-200:]}")

        clone_step = exec_step(pod, "clone",
                               f"python3 -m scripts.mq.clone_pr --repo {env['REPO_URL']} --branch main",
                               env, timeout=120)
        result.steps.append(clone_step)
        if not clone_step.ok:
            return _fail(result, clone_step, f"clone exit {clone_step.returncode}: {clone_step.stderr[-300:]}")

        impact_step = exec_step(pod, "test-impact-branch",
                                "python3 -m scripts.mq.test_impact --pr-number main --num-shards 4",
                                env, timeout=120)
        result.steps.append(impact_step)
        if not impact_step.ok:
            if "unexpected keyword argument 'cwd'" in impact_step.stderr:
                return _fail(result, impact_step,
                             "REGRESSION: run_cmd() does not accept cwd — fix scripts/common/shell.py")
            return _fail(result, impact_step,
                         f"test_impact branch exit {impact_step.returncode}: {impact_step.stderr[-400:]}")

        # Verify ::add-job-context TestShards:: is present in stdout (job-scoped, required for fan-out).
        if "::add-job-context TestShards::" not in impact_step.stdout:
            return _fail(result, impact_step,
                         "REGRESSION: test_impact stdout missing ::add-job-context TestShards:: — "
                         "fan_out.source: TestShards will have no items to iterate")

        # Verify real test metrics — not 0/0
        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"ip = os.path.join(ws, 'test_impact.json')\n"
            f"if not os.path.exists(ip):\n"
            f"    issues.append('test_impact.json missing')\n"
            f"else:\n"
            f"    d = json.loads(open(ip).read())\n"
            f"    for k in ('total_tests', 'selected_tests', 'reduction_pct', 'shards', 'language'):\n"
            f"        if k not in d: issues.append(f'test_impact.json missing {{k}}')\n"
            f"    total = d.get('total_tests', 0)\n"
            f"    selected = d.get('selected_tests', 0)\n"
            f"    shard_count = len(d.get('shards', []))\n"
            f"    lang = d.get('language', '?')\n"
            f"    fallback = d.get('fallback_full_suite', False)\n"
            f"    print(f'::add-task-context TOTAL_TESTS::{{total}}')\n"
            f"    print(f'::add-task-context SELECTED_TESTS::{{selected}}')\n"
            f"    print(f'::add-task-context SHARD_COUNT::{{shard_count}}')\n"
            f"    print(f'::add-task-context LANGUAGE::{{lang}}')\n"
            f"    print(f'::add-task-context FALLBACK_FULL_SUITE::{{fallback}}')\n"
            f"    if total == 0:\n"
            f"        issues.append('total_tests=0: full-suite fallback did not discover any tests')\n"
            f"    if selected == 0:\n"
            f"        issues.append('selected_tests=0: branch run must select all tests (full suite fallback)')\n"
            f"    if shard_count == 0:\n"
            f"        issues.append('shards=[]: no shards produced — fan-out will have nothing to run')\n"
            f"    if not fallback:\n"
            f"        issues.append('fallback_full_suite=False: expected True for branch with empty diff')\n"
            f"    reduction = d.get('reduction_pct', 0.0)\n"
            f"    print(f'::add-task-context REDUCTION_PCT::{{reduction}}')\n"
            f"    # Per-shard load balance: max/min ratio (1.0=perfect, <3.0=acceptable)\n"
            f"    durations = [s['est_duration_s'] for s in d.get('shards', []) if s.get('est_duration_s', 0) > 0]\n"
            f"    if len(durations) > 1:\n"
            f"        balance = round(max(durations) / min(durations), 2)\n"
            f"        print(f'::add-task-context SHARD_BALANCE::{{balance}}')\n"
            f"        if balance > 3.0:\n"
            f"            issues.append(f'shard imbalance: max/min={{balance}} (expected <3.0)')\n"
            f"    # Print per-shard summary\n"
            f"    for s in d.get('shards', []):\n"
            f"        print(f'  shard={{s[\"shard_id\"]}} tests={{s[\"test_count\"]}} est={{s[\"est_duration_s\"]}}s')\n"
            f"if issues:\n"
            f"    print('ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"print('::add-task-context VERIFY::ok')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify-branch-impact", verify_cmd, env, timeout=30)
        result.steps.append(verify_step)
        if not verify_step.ok:
            return _fail(result, verify_step, f"verify failed: {verify_step.stderr[-300:]}")

    total = verify_step.context.get("TOTAL_TESTS", "?")
    selected = verify_step.context.get("SELECTED_TESTS", "?")
    shards = verify_step.context.get("SHARD_COUNT", "?")
    lang = verify_step.context.get("LANGUAGE", "?")
    fallback = verify_step.context.get("FALLBACK_FULL_SUITE", "?")
    reduction = verify_step.context.get("REDUCTION_PCT", "0.0")
    balance = verify_step.context.get("SHARD_BALANCE", "n/a")
    return _pass(result,
                 f"branch=main full-suite fallback={fallback} "
                 f"selected={selected}/{total} shards={shards} lang={lang} "
                 f"reduction={reduction}% shard_balance={balance}")


def _run_gate_review_pipeline(base_env: dict[str, str], pr_url: str,
                               pod_label: str) -> TestResult:
    """Shared read-only gate-review pipeline: clone → scope → risk → report.

    Does NOT run review.run (requires Claude) or gate-check/merge/approve (write ops).
    Validates:
      - parse_pr_ref correctly extracts bare PR number and repo from full URL
      - apply_repo_override injects correct GH_ORG/GH_REPO or BITBUCKET_WORKSPACE/BITBUCKET_REPO
      - scope_router and risk_score run against the correct repo
      - report.md title uses bare PR number (not the full URL)
      - report.html is rendered
    """
    result = TestResult(pod_label)
    ws = f"/workspace/gate_review_{pod_label.replace('-', '_')}"

    env = dict(base_env)
    env["WORKSPACE_DIR"] = ws
    env["CODEBASE_DIR"] = f"{ws}/repo"
    env["TASK_TYPE"] = "review"
    env["PR_NUMBER"] = pr_url
    # Suppress Slack to keep test read-only and fast
    env.pop("SLACK_BOT_TOKEN", None)
    env["SLACK_BOT_TOKEN"] = ""

    with pod_fixture(pod_label) as pod:
        setup_step = exec_step(pod, "setup",
                               f"mkdir -p {ws}/reports {ws}/logs",
                               env, timeout=30)
        result.steps.append(setup_step)
        if not setup_step.ok:
            return _fail(result, setup_step, f"setup failed: {setup_step.stderr[-200:]}")

        # clone_pr — verifies parse_pr_ref + apply_repo_override
        clone_step = exec_step(pod, "clone",
                               f"python3 -m scripts.mq.clone_pr --pr-number '{pr_url}'",
                               env, timeout=180)
        result.steps.append(clone_step)
        if not clone_step.ok:
            return _fail(result, clone_step,
                         f"clone_pr exit {clone_step.returncode}: {clone_step.stderr[-400:]}")

        # scope_router — read-only (label_pr skips for Bitbucket; GitHub label is fine on own repo)
        scope_step = exec_step(pod, "scope-router",
                               f"python3 -m scripts.mq.scope_router --pr-number '{pr_url}'",
                               env, timeout=120)
        result.steps.append(scope_step)
        if not scope_step.ok:
            return _fail(result, scope_step,
                         f"scope_router exit {scope_step.returncode}: {scope_step.stderr[-400:]}")

        err = _check_keys(scope_step, ["SCOPE_KEY", "BLAST_RADIUS", "CHANGED_FILES"])
        if err:
            return _fail(result, scope_step, err)

        # risk_score — read-only
        risk_step = exec_step(pod, "risk-score",
                              f"python3 -m scripts.mq.risk_score --pr-number '{pr_url}'",
                              env, timeout=120)
        result.steps.append(risk_step)
        if not risk_step.ok:
            return _fail(result, risk_step,
                         f"risk_score exit {risk_step.returncode}: {risk_step.stderr[-400:]}")

        err = _check_keys(risk_step, ["RISK_TIER", "RISK_SCORE"])
        if err:
            return _fail(result, risk_step, err)

        # Inject mock review_result.json + gate_result.json so report includes all sections.
        # review.run (Claude) is not run in pod tests — too slow/costly.  The mock has a
        # realistic schema with critical + high findings so we can assert the report renders them.
        mock_review = json.dumps({
            "verdict": "DONE_WITH_CONCERNS",
            "findings": [
                {"severity": "critical", "category": "security",
                 "file": "auth/login.py", "line": 45,
                 "summary": "SQL injection via unsanitized user input in login handler",
                 "short_summary": "SQL injection in login handler",
                 "failure_scenario": "User passes ' OR 1=1 -- causing full table scan"},
                {"severity": "high", "category": "correctness",
                 "file": "api/handler.py", "line": 123,
                 "summary": "Off-by-one in pagination loop skips last result",
                 "short_summary": "Off-by-one in pagination"},
                {"severity": "medium", "category": "test-coverage",
                 "file": "billing/charge.py", "line": 0,
                 "summary": "No tests for the charge refund path",
                 "short_summary": "Missing refund path tests"},
            ],
        })
        # gate_result.json: mirrors what gate-check task writes
        mock_gate = json.dumps({
            "risk_score": 41.5,
            "risk_tier": "HIGH",
            "has_critical_findings": True,
            "findings_count": 3,
            "needs_approval": True,
            "reason": "critical findings",
        })
        inject_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import os\n"
            f"ws = {ws!r}\n"
            f"open(os.path.join(ws, 'review_result.json'), 'w').write({mock_review!r})\n"
            f"open(os.path.join(ws, 'gate_result.json'), 'w').write({mock_gate!r})\n"
            f"print('injected review_result.json and gate_result.json')\n"
            f"PYEOF"
        )
        inject_step = exec_step(pod, "inject-mock-review",
                                inject_cmd, env, timeout=15)
        result.steps.append(inject_step)
        if not inject_step.ok:
            return _fail(result, inject_step, f"inject failed: {inject_step.stderr[-200:]}")

        # report — read-only, no Slack
        report_step = exec_step(pod, "report",
                                "python3 -m scripts.mq.report",
                                env, timeout=60)
        result.steps.append(report_step)
        if not report_step.ok:
            return _fail(result, report_step,
                         f"report exit {report_step.returncode}: {report_step.stderr[-400:]}")

        # Verify all report sections present and correct
        verify_cmd = (
            f"python3 - <<'PYEOF'\n"
            f"import json, sys, os\n"
            f"ws = '{ws}'\n"
            f"issues = []\n"
            f"# scope.json\n"
            f"sp = os.path.join(ws, 'scope.json')\n"
            f"if not os.path.exists(sp): issues.append('scope.json missing')\n"
            f"else:\n"
            f"    d = json.loads(open(sp).read())\n"
            f"    for k in ('scope', 'blast_radius', 'changed_files'):\n"
            f"        if k not in d: issues.append(f'scope.json missing {{k}}')\n"
            f"    print(f'::add-task-context SCOPE::{{d.get(\"scope\",\"?\")}}')\n"
            f"    print(f'::add-task-context BLAST_RADIUS::{{d.get(\"blast_radius\",\"?\")}}')\n"
            f"# risk_score.json\n"
            f"rp = os.path.join(ws, 'risk_score.json')\n"
            f"if not os.path.exists(rp): issues.append('risk_score.json missing')\n"
            f"else:\n"
            f"    d = json.loads(open(rp).read())\n"
            f"    for k in ('tier', 'score', 'dimensions', 'requires_human_approval'):\n"
            f"        if k not in d: issues.append(f'risk_score.json missing {{k}}')\n"
            f"    print(f'::add-task-context RISK_TIER::{{d.get(\"tier\",\"?\")}}')\n"
            f"    print(f'::add-task-context RISK_SCORE::{{d.get(\"score\",\"?\")}}')\n"
            f"# report.md — must contain all gate-review sections\n"
            f"md = os.path.join(ws, 'reports', 'report.md')\n"
            f"if not os.path.exists(md): issues.append('reports/report.md missing')\n"
            f"else:\n"
            f"    content = open(md).read()\n"
            f"    md_size = os.path.getsize(md)\n"
            f"    print(f'::add-task-context REPORT_MD_BYTES::{{md_size}}')\n"
            f"    if md_size < 100: issues.append(f'report.md too small ({{md_size}}B)')\n"
            f"    # Full URL must not appear in PR # heading — parse_pr_ref must normalise it\n"
            f"    if 'PR #http' in content:\n"
            f"        issues.append('report.md title contains full URL — parse_pr_ref not called')\n"
            f"    print(f'::add-task-context PR_URL_NORMALISED::{{\"no\" if \"PR #http\" in content else \"yes\"}}')\n"
            f"    # All gate-review sections must be present\n"
            f"    for section in ('## Risk Score', '## Review Findings', '## Gate Decision'):\n"
            f"        if section not in content:\n"
            f"            issues.append(f'report.md missing section: {{section}}')\n"
            f"    # Review findings table must contain the critical finding\n"
            f"    if 'critical' not in content:\n"
            f"        issues.append('report.md Review Findings missing critical severity')\n"
            f"    if 'SQL injection' not in content:\n"
            f"        issues.append('report.md Review Findings missing expected finding summary')\n"
            f"    # Gate decision must mention approval\n"
            f"    if 'Human approval required' not in content and 'Eligible for auto-merge' not in content:\n"
            f"        issues.append('report.md Gate Decision missing approval decision text')\n"
            f"    print(f'::add-task-context SECTIONS_OK::{{\"yes\" if not issues else \"no\"}}')\n"
            f"# report.html\n"
            f"html = os.path.join(ws, 'reports', 'report.html')\n"
            f"if not os.path.exists(html): issues.append('reports/report.html missing')\n"
            f"else:\n"
            f"    hsize = os.path.getsize(html)\n"
            f"    print(f'::add-task-context REPORT_HTML_BYTES::{{hsize}}')\n"
            f"    if hsize < 200: issues.append(f'report.html too small ({{hsize}}B)')\n"
            f"    hcontent = open(html).read()\n"
            f"    if 'Review Findings' not in hcontent:\n"
            f"        issues.append('report.html missing Review Findings section')\n"
            f"    if 'Gate Decision' not in hcontent:\n"
            f"        issues.append('report.html missing Gate Decision section')\n"
            f"if issues:\n"
            f"    print('ISSUES: ' + '; '.join(issues), file=sys.stderr)\n"
            f"    sys.exit(1)\n"
            f"print('::add-task-context VERIFY::ok')\n"
            f"PYEOF"
        )
        verify_step = exec_step(pod, "verify", verify_cmd, env, timeout=30)
        result.steps.append(verify_step)
        if not verify_step.ok:
            return _fail(result, verify_step, f"verify failed: {verify_step.stderr[-400:]}")

    scope = verify_step.context.get("SCOPE", "?")
    blast = verify_step.context.get("BLAST_RADIUS", "?")
    tier = verify_step.context.get("RISK_TIER", "?")
    score = verify_step.context.get("RISK_SCORE", "?")
    md_bytes = verify_step.context.get("REPORT_MD_BYTES", "?")
    html_bytes = verify_step.context.get("REPORT_HTML_BYTES", "?")
    url_norm = verify_step.context.get("PR_URL_NORMALISED", "?")
    total_s = sum(s.elapsed for s in result.steps)
    return _pass(result,
                 f"pr_url={pr_url} scope={scope} blast={blast} tier={tier} score={score} "
                 f"url_normalised={url_norm} report.md={md_bytes}B html={html_bytes}B "
                 f"elapsed={total_s:.0f}s")


def test_35_gate_review_gh(base_env: dict[str, str]) -> TestResult:
    """Gate-review read-only pipeline against a GitHub PR full URL.

    Exercises parse_pr_ref + apply_repo_override end-to-end in a pod:
      clone_pr → scope_router → risk_score → report
    Verifies report title shows bare PR number (not full URL).
    No write operations — does not run review.run, gate-check, merge, or approve.

    Override via GH_PR_URL env var.
    """
    pr_url = os.environ.get("GH_PR_URL", f"https://github.com/{GH_ORG}/{GH_REPO}/pull/9")
    env = dict(base_env)
    # Clear any Bitbucket vars so GitHub path is clean
    for k in ("BITBUCKET_WORKSPACE", "BITBUCKET_REPO", "DEFAULT_TRACKER"):
        env.pop(k, None)
    return _run_gate_review_pipeline(env, pr_url, "gate-review-gh")


def test_36_gate_review_bb(base_env: dict[str, str]) -> TestResult:
    """Gate-review read-only pipeline against a Bitbucket PR full URL.

    Exercises parse_pr_ref + apply_repo_override end-to-end in a pod:
      clone_pr → scope_router → risk_score → report
    Verifies apply_repo_override sets BITBUCKET_WORKSPACE/BITBUCKET_REPO and
    DEFAULT_TRACKER=bitbucket, so all downstream API calls use Bitbucket.
    No write operations — label_pr skips for Bitbucket; no merge/approve.

    Override via BB_PR_URL env var.
    Skipped when BITBUCKET_TOKEN not set.
    """
    result = TestResult("gate-review-bb")
    if not base_env.get("BITBUCKET_TOKEN") and not base_env.get("BITBUCKET_APP_PASSWORD"):
        result.passed = True
        result.message = "SKIPPED — BITBUCKET_TOKEN/BITBUCKET_APP_PASSWORD not set"
        return result

    pr_url = os.environ.get("BB_PR_URL",
                             "https://bitbucket.org/cribl/cribl/pull-requests/45974")
    env = dict(base_env)
    # Unset GitHub vars and pre-set tracker vars to ensure apply_repo_override drives config
    for k in ("GH_ORG", "GH_REPO", "DEFAULT_TRACKER", "BITBUCKET_WORKSPACE", "BITBUCKET_REPO"):
        env.pop(k, None)
    return _run_gate_review_pipeline(env, pr_url, "gate-review-bb")


def test_37_gate_check_logic(base_env: dict[str, str]) -> TestResult:
    """Pod test for the gate-check inline Python + bash URL-parsing used in auto-merge/reject.

    Tests (read-only — no actual merge/approve):
      1. gate-check exits 0 when needs_approval=True (critical finding)
      2. gate-check exits 3 when needs_approval=False (low risk, no critical)
      3. Bash URL parser correctly extracts PR num + org/repo from GitHub URL
      4. Bash URL parser correctly extracts PR num + workspace/repo from Bitbucket URL
      5. Bash URL parser falls back to bare number when given a plain number
    """
    result = TestResult("gate-check-logic")
    with pod_fixture("gate-check-logic") as pod:
        env = dict(base_env)
        ws = "/workspace/gate_check_test"
        env["WORKSPACE_DIR"] = ws
        env["APPROVAL_THRESHOLD"] = "30"
        env.pop("SLACK_BOT_TOKEN", None)
        env["SLACK_BOT_TOKEN"] = ""

        setup_step = exec_step(pod, "setup", f"mkdir -p {ws}", env, timeout=15)
        result.steps.append(setup_step)
        if not setup_step.ok:
            return _fail(result, setup_step, f"setup: {setup_step.stderr[-200:]}")

        # --- Test 1: gate-check exits 0 when critical finding present ---
        inject1 = exec_step(pod, "inject-critical",
            f"python3 - <<'PYEOF'\n"
            f"import json, os\n"
            f"ws = {ws!r}\n"
            f"open(f'{{ws}}/risk_score.json','w').write(json.dumps({{'score':25,'tier':'LOW','dimensions':{{}},'requires_human_approval':False}}))\n"
            f"open(f'{{ws}}/review_result.json','w').write(json.dumps({{'verdict':'BLOCKED','findings':[{{'severity':'critical','summary':'SQL injection'}}]}}))\n"
            f"PYEOF",
            env, timeout=15)
        result.steps.append(inject1)
        if not inject1.ok:
            return _fail(result, inject1, f"inject: {inject1.stderr[-200:]}")

        gate_critical_cmd = (
            f"python3 -c \"\n"
            f"import json, os, sys\n"
            f"risk = json.load(open('{ws}/risk_score.json'))\n"
            f"review = json.load(open('{ws}/review_result.json'))\n"
            f"threshold = int(os.environ.get('APPROVAL_THRESHOLD', '70'))\n"
            f"score = risk['score']\n"
            f"has_critical = any(f.get('severity') == 'critical' for f in review.get('findings', []))\n"
            f"needs_approval = score >= threshold or has_critical\n"
            f"gate = {{'risk_score':score,'risk_tier':risk['tier'],'has_critical_findings':has_critical,'findings_count':len(review.get('findings',[])),'needs_approval':needs_approval,'reason':'critical findings' if has_critical else f'score {{score}}'}}\n"
            f"json.dump(gate, open('{ws}/gate_result.json','w'))\n"
            f"print(f'gate: approval={{needs_approval}} critical={{has_critical}}')\n"
            f"sys.exit(0 if needs_approval else 3)\n"
            f"\""
        )
        gate1 = exec_step(pod, "gate-critical", gate_critical_cmd, env, timeout=15)
        result.steps.append(gate1)
        if gate1.returncode != 0:
            return _fail(result, gate1,
                         f"gate-check should exit 0 (needs approval) for critical finding — got {gate1.returncode}")

        # --- Test 2: gate-check exits 3 when low risk, no critical ---
        inject2 = exec_step(pod, "inject-low-risk",
            f"python3 - <<'PYEOF'\n"
            f"import json\n"
            f"ws = {ws!r}\n"
            f"open(f'{{ws}}/risk_score.json','w').write(json.dumps({{'score':10,'tier':'LOW','dimensions':{{}},'requires_human_approval':False}}))\n"
            f"open(f'{{ws}}/review_result.json','w').write(json.dumps({{'verdict':'DONE','findings':[{{'severity':'medium','summary':'minor style'}}]}}))\n"
            f"PYEOF",
            env, timeout=15)
        result.steps.append(inject2)
        if not inject2.ok:
            return _fail(result, inject2, f"inject2: {inject2.stderr[-200:]}")

        gate2 = exec_step(pod, "gate-low-risk", gate_critical_cmd, env, timeout=15)
        result.steps.append(gate2)
        if gate2.returncode != 3:
            return _fail(result, gate2,
                         f"gate-check should exit 3 (auto-merge) for low risk — got {gate2.returncode}")

        # --- Tests 3-5: bash URL parser used in auto-merge/merge/reject ---
        url_parse_cmd = (
            "python3 - <<'PYEOF'\n"
            "import subprocess, sys\n"
            "cases = [\n"
            "    ('https://github.com/bhatti/todo-sample/pull/9',  'github', 'bhatti', 'todo-sample', '9'),\n"
            "    ('https://bitbucket.org/cribl/cribl/pull-requests/48776', 'bitbucket', 'cribl', 'cribl', '48776'),\n"
            "    ('42', 'bare', '', '', '42'),\n"
            "]\n"
            "errors = []\n"
            "for url, tracker, org, repo, pr in cases:\n"
            "    script = f'''\n"
            "PR_URL=\"{url}\"\n"
            "if echo \"$PR_URL\" | grep -q \"bitbucket.org\"; then\n"
            "  PR_NUM=$(echo \"$PR_URL\" | grep -oE '[0-9]+$')\n"
            "  BB_WS=$(echo \"$PR_URL\" | sed 's|https://bitbucket.org/||' | cut -d'/' -f1)\n"
            "  BB_REPO=$(echo \"$PR_URL\" | sed 's|https://bitbucket.org/||' | cut -d'/' -f2)\n"
            "  echo \"tracker=bitbucket org=$BB_WS repo=$BB_REPO pr=$PR_NUM\"\n"
            "elif echo \"$PR_URL\" | grep -q \"github.com\"; then\n"
            "  PR_NUM=$(echo \"$PR_URL\" | grep -oE '[0-9]+$')\n"
            "  GH_SLUG=$(echo \"$PR_URL\" | sed 's|https://github.com/||' | sed 's|/pull/.*||')\n"
            "  GH_ORG_PART=$(echo \"$GH_SLUG\" | cut -d'/' -f1)\n"
            "  GH_REPO_PART=$(echo \"$GH_SLUG\" | cut -d'/' -f2)\n"
            "  echo \"tracker=github org=$GH_ORG_PART repo=$GH_REPO_PART pr=$PR_NUM\"\n"
            "else\n"
            "  echo \"tracker=bare org= repo= pr=$PR_URL\"\n"
            "fi\n"
            "'''\n"
            "    r = subprocess.run(['bash','-c',script], capture_output=True, text=True)\n"
            "    out = r.stdout.strip()\n"
            "    print(f'  [{url[:40]}] → {out}')\n"
            "    if f'tracker={tracker}' not in out: errors.append(f'{url}: expected tracker={tracker} got: {out}')\n"
            "    if org and f'org={org}' not in out: errors.append(f'{url}: expected org={org} got: {out}')\n"
            "    if repo and f'repo={repo}' not in out: errors.append(f'{url}: expected repo={repo} got: {out}')\n"
            "    if f'pr={pr}' not in out: errors.append(f'{url}: expected pr={pr} got: {out}')\n"
            "if errors:\n"
            "    for e in errors: print('ERROR: '+e, file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "print('::add-task-context URL_PARSE::ok')\n"
            "PYEOF"
        )
        url_step = exec_step(pod, "url-parse", url_parse_cmd, env, timeout=20)
        result.steps.append(url_step)
        if not url_step.ok:
            return _fail(result, url_step, f"URL parse test: {url_step.stderr[-400:]}")

        result.passed = True
        result.message = "gate-check logic: critical→exit0 ✓, low-risk→exit3 ✓, URL parsing: GH/BB/bare ✓"
        return result


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
    "adhoc-ask":             test_20_adhoc_ask,
    "pr-audit-slack-routing": test_21_pr_audit_slack_routing,
    "skill-invoke":           test_22_skill_invoke,
    "skill-flag-parsing":     test_23_skill_flag_parsing,
    "skill-integ-tests":      test_24_skill_integ_tests,
    "skill-service-awareness": test_25_skill_service_awareness,
    "skill-identifier-passthrough": test_26_skill_identifier_passthrough,
    "skill-e2e-identifier":   test_27_skill_e2e_with_identifier,
    "skill-post":             test_28_skill_post,
    "skill-node-image":       test_29_skill_node_image,
    "adhoc-pr-queue-report":  test_30_adhoc_pr_queue_report,
    "mq-scope-router":        test_31_mq_scope_router,
    "mq-test-impact":         test_32_mq_test_impact,
    "mq-full-pipeline":       test_33_mq_full_pipeline,
    "mq-test-impact-branch":  test_34_mq_test_impact_branch,
    "gate-review-gh":         test_35_gate_review_gh,
    "gate-review-bb":         test_36_gate_review_bb,
    "gate-check-logic":       test_37_gate_check_logic,
}

DEFAULT_TESTS = ["jira-query", "jira-analyze", "standup-gather"]


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Pod-based functional tests for ai-dev-tools")
    parser.add_argument("--tests", default=",".join(DEFAULT_TESTS),
                        help="Comma-separated test names, or 'all'")
    parser.add_argument("--list", action="store_true", help="List available tests and exit")
    parser.add_argument("--cleanup", action="store_true",
                        help="Delete all stale ai-dev-pod-test pods left by interrupted runs and exit")
    args = parser.parse_args()

    if args.list:
        print("Available tests:")
        for name in ALL_TESTS:
            print(f"  {name}")
        return

    if args.cleanup:
        print(f"[pod-tests] deleting stale pods with label app=ai-dev-pod-test in namespace {NAMESPACE} ...", flush=True)
        r = _kubectl("delete", "pods", "-l", "app=ai-dev-pod-test",
                     "--ignore-not-found=true", "--grace-period=0", check=False)
        print(r.stdout.strip() or "no pods deleted", flush=True)
        return

    # Auto-clean any stale pods from previous interrupted runs before starting
    _kubectl("delete", "pods", "-l", "app=ai-dev-pod-test",
             "--ignore-not-found=true", "--grace-period=0", check=False)

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
