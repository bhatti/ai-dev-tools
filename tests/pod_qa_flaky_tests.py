"""Pod functional tests for the flaky-test and QA tooling (scripts/flaky, scripts/qa, scripts/common/pr_context).

Registered into tests/test_pod_functional.py's ALL_TESTS via build_tests(). They run the real scripts in a
real pod (local scripts copied over the image), with no LLM involved:

    PYTHONPATH=. python3 tests/test_pod_functional.py --tests flaky-pipeline,qa-verify-url,qa-regression-url

Names: flaky-pipeline, qa-pr-context-gh, qa-pr-context-bb, qa-verify-url, qa-regression-url, qa-worktree-env,
       qa-docker-preflight-no-daemon, qa-regression-docker and skill-dind-real (real docker:dind sidecar), qa-skills-load.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from pathlib import Path
from types import ModuleType

NEW_SCRIPTS = [
    "scripts/common/pr_context.py", "scripts/common/redact.py", "scripts/common/docker_check.py", "scripts/common/report_bundle.py",
    "scripts/common/env_scrub.py", "scripts/common/exit_codes.py", "scripts/common/git_utils.py", "scripts/mq/_shared.py",
    "scripts/flaky/__init__.py", "scripts/flaky/detect_runner.py", "scripts/flaky/identify.py", "scripts/flaky/rerun.py",
    "scripts/flaky/classify.py", "scripts/flaky/bisect_polluter.py", "scripts/flaky/report.py",
    "scripts/qa/__init__.py", "scripts/qa/model.py", "scripts/qa/delta.py", "scripts/qa/report.py",
    "scripts/qa/env_manager.py", "scripts/qa/ui_runner.py", "scripts/qa/helpers.sh",
    "scripts/common/report_renderer.py", "scripts/common/report_utils.py", "scripts/common/bitbucket_api.py",
    "scripts/analyze/pr_fetcher.py", "scripts/common/config.py", "scripts/skill/run_skill.py", "scripts/skill/flags.py",
]
YGS_SKILLS = ["ygs-qa", "ygs-flaky-test", "shared"]
YGS_REPO = Path(os.environ.get("YGS_SKILLS_DIR") or Path(__file__).resolve().parents[2] / "you-got-skills")
GH_PR_URL = os.environ.get("QA_TEST_GH_PR_URL", "https://github.com/bhatti/todo-sample/pull/24")
# A readable Bitbucket PR (optional). Without it the test exercises the real REST error path with a PR that does not exist.
BB_PR_URL = os.environ.get("QA_TEST_BB_PR_URL", "")
BB_PLACEHOLDER_URL = "https://bitbucket.org/example-org/example-repo/pull-requests/1"

# A tiny HTTP app with switchable behavior; two instances stand in for baseline and candidate.
FIXTURE_APP = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
FEATURE, BREAK = os.environ.get("FEATURE") == "1", os.environ.get("BREAK") == "1"
if os.environ.get("QA_LEAK") == "1": sys.stderr.write("startup config token=abcdefghijkl123 loaded\n")
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): sys.stderr.write("access " + (a[0] % a[1:]) + "\n")
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path == "/api/health": self._send(200, {"ok": True})
        elif self.path == "/api/items":
            if BREAK:
                sys.stderr.write("ERROR items handler crashed: KeyError 'sku' at request 42\n"); self._send(500, {"error": "boom"})
            else: self._send(200, {"items": [1, 2]})
        elif self.path == "/api/limit-check": self._send(409 if FEATURE else 200, {"limit": 10 if FEATURE else None})
        elif self.path == "/ui":
            msg = '<div id="msg">Cart limit reached</div>' if FEATURE else ""
            self._send(200, ("<html><head><title>Shop</title></head><body><h1>Cart</h1>%s</body></html>" % msg).encode(), "text/html")
        elif self.path == "/": self._send(200, b"<html><body>home</body></html>", "text/html")
        else: self._send(404, {"error": "not found"})
    def do_POST(self): self._send(201, {"created": True})
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''

PLAN = {
    "criteria": [{"id": "AC1", "text": "Listing items keeps working", "source": "pr_description"},
                 {"id": "AC2", "text": "The API rejects over-limit carts with 409", "source": "issue:SHOP-1"},
                 {"id": "AC3", "text": "The UI shows 'Cart limit reached'", "source": "issue:SHOP-1"}],
    "cases": [
        {"id": "C1", "ac": ["AC1"], "type": "API", "title": "list items", "expected": "200"},
        {"id": "C2", "ac": ["AC2"], "type": "API", "title": "limit check", "expected": "409"},
        {"id": "C3", "ac": ["AC3"], "type": "UI", "title": "limit banner", "expected": "banner visible"},
        {"id": "C4", "ac": ["AC1"], "type": "API", "title": "health", "expected": "200"},
        {"id": "C5", "ac": ["AC2"], "type": "API", "title": "create item", "expected": "201", "mutating": True},
    ],
}
API_TESTS = r'''
source /app/scripts/qa/helpers.sh
qa_case C1; api_call GET /api/items; check_http 200 "list items"
qa_case C2; api_call GET /api/limit-check; check_http 409 "over-limit cart rejected"
qa_case C4; api_call GET /api/health; check_http 200 "health"
qa_case C5; api_call POST /api/items -d '{}'; check_http 201 "create item"
qa_summary
'''
UI_SPEC = {"smoke": ["/"], "cases": [{"id": "C3", "steps": [{"goto": "/ui"}, {"expect_text": "Cart limit reached"}, {"screenshot": "banner"}]}]}
RUN_ALL = r"""
bash /workspace/qa/api-tests.sh
python3 -m scripts.qa.ui_runner --spec /workspace/qa/ui-spec.json --out-dir "$QA_OUT"
python3 -c "import os; open(os.environ['QA_OUT'] + '/seen-env.txt', 'w').write(str(os.environ.get('GH_TOKEN')) + ' ' + str(os.environ.get('QA_BASE_URL')))"
"""

# Mirrors the pod formicary builds for docs/examples/ai-skill.yaml when --dind is used: host network,
# privileged main container, DOCKER_HOST/DOCKER_TLS_CERTDIR in the main env, and a docker:dind sidecar started
# through `env -u DOCKER_HOST dockerd-entrypoint.sh` with the YAML's resource settings.
_POD_MANIFEST_DIND = """\
apiVersion: v1
kind: Pod
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app: ai-dev-pod-test
spec:
  restartPolicy: Never
  hostNetwork: true
  containers:
  - name: main
    image: {image}
    imagePullPolicy: Always
    command: ["sleep", "3600"]
    envFrom:
    - secretRef:
        name: ai-dev-credentials
    env:
    - name: DOCKER_HOST
      value: tcp://localhost:2375
    - name: DOCKER_TLS_CERTDIR
      value: ""
    securityContext:
      privileged: true
    resources:
      requests: {{memory: 1G, cpu: "2"}}
      limits: {{memory: 24G}}
    volumeMounts:
    - name: workspace
      mountPath: /workspace
  - name: docker-dind
    image: docker:dind
    imagePullPolicy: IfNotPresent
    command: ["env", "-u", "DOCKER_HOST", "dockerd-entrypoint.sh"]
    env:
    - name: DOCKER_TLS_CERTDIR
      value: ""
    securityContext:
      privileged: true
    resources:
      requests: {{cpu: 500m}}
      limits: {{memory: 8G}}
  volumes:
  - name: workspace
    emptyDir: {{}}
"""


def build_tests(h: ModuleType) -> dict:
    """Return {name: test_fn}. ``h`` is the test_pod_functional module (pod_fixture, exec_step, ...)."""
    for rel in NEW_SCRIPTS:
        if rel not in h._SCRIPTS_TO_COPY:
            h._SCRIPTS_TO_COPY.append(rel)

    def stage(pod: str, rel_target: str, content: str, env: dict) -> None:
        """Write a file into the pod through a quoted heredoc (no local temp files, no quoting surprises)."""
        marker = f"EOF_{uuid.uuid4().hex[:8]}"
        step = h.exec_step(pod, f"stage-{Path(rel_target).name}", f"mkdir -p $(dirname {rel_target}) && cat > {rel_target} <<'{marker}'\n{content}\n{marker}", env, timeout=30)
        if step.returncode != 0:
            raise RuntimeError(f"could not stage {rel_target}: {step.stderr[-200:]}")

    def assert_dind_port_free() -> None:
        """hostNetwork makes dockerd's port 2375 node-wide, exactly as in a real job; two dind pods would collide."""
        out = h._kubectl("get", "pods", "-o", "json", timeout=30).stdout
        for pod in json.loads(out).get("items", []):
            running = pod.get("status", {}).get("phase") == "Running"
            has_dind = any(c.get("image", "").startswith("docker:") for c in pod["spec"].get("containers", []))
            if running and has_dind and pod["spec"].get("hostNetwork"):
                raise RuntimeError(f"pod {pod['metadata']['name']} already runs a hostNetwork docker:dind on port 2375; retry when it has finished")

    @contextlib.contextmanager
    def dind_pod(test_name: str):
        assert_dind_port_free()
        name = f"ai-dev-{test_name.replace('_', '-')[:20]}-{uuid.uuid4().hex[:6]}"
        print(f"\n  [pod] creating {name} with docker:dind sidecar ...", flush=True)
        h._create_pod(name, image=h.IMAGE, manifest_template=_POD_MANIFEST_DIND)
        try:
            h._copy_scripts(name)
            yield name
        finally:
            print(f"  [pod] deleting {name} ...", flush=True)
            h._delete_pod(name)

    def markers_ok(result, step, expected: dict[str, str], required_text: list[str] = ()):
        err = h._check_values(step, expected)
        if err:
            return h._fail(result, step, err)
        for needle in required_text:
            if needle not in step.stdout:
                return h._fail(result, step, f"expected text not in output: {needle!r}\n  tail: {step.stdout[-500:]}")
        return None

    # ── flaky ────────────────────────────────────────────────────────────────────

    def test_flaky_pipeline(base_env):
        result = h.TestResult("flaky-pipeline")
        env = dict(base_env)
        with h.pod_fixture("flaky-pipeline") as pod:
            script = r'''
set -u
W=/workspace/flaky_pipeline; rm -rf $W; mkdir -p $W/proj $W/pass $W/pol
cat > $W/proj/flaky_test.py <<'PYEOF'
import pathlib, sys, time
c = pathlib.Path("/workspace/flaky_pipeline/counter")
n = int(c.read_text()) + 1 if c.exists() else 1
c.write_text(str(n))
time.sleep(0.05)
if n % 3 == 0:
    print("FAILED flaky_test.py::test_x - AssertionError: expected 1 got", n); sys.exit(1)
print("1 passed")
PYEOF
touch $W/proj/conftest.py
cd /app
python3 -m scripts.flaky.detect_runner --repo $W/proj --file flaky_test.py --name test_x > $W/detect.json
echo "::add-task-context DETECTED::$(python3 -c "import json;print(json.load(open('$W/detect.json'))['runner'])")"
python3 -m scripts.flaky.rerun --cwd $W/proj --cmd "python3 flaky_test.py" --runs 12 --out $W/runs.json
python3 -m scripts.flaky.classify --runs $W/runs.json --source $W/proj/flaky_test.py --out $W/findings.json
echo "::add-task-context SLEEP_FINDING::$(python3 -c "import json;f=json.load(open('$W/findings.json'))['findings'];print(any(x['id']=='fixed_sleep' and x['line']==5 for x in f))")"
python3 -m scripts.flaky.report --runs $W/runs.json --findings $W/findings.json --test flaky_test.py --rev abc123 --out-dir $W/reports | tail -1 > $W/status.json
echo "::add-task-context STATUS::$(python3 -c "import json;print(json.load(open('$W/status.json'))['status'])")"
python3 -c "print('::add-task-context REPORT_HEADLINE::' + str('FLAKY: intermittent failures reproduced' in open('$W/reports/report.md').read()))"
# cannot reproduce: 10 passing runs, then the same via an unusable runner
printf 'print("3 passed")\n' > $W/pass/ok.py
python3 -m scripts.flaky.rerun --cwd $W/pass --cmd "python3 ok.py" --runs 10 --out $W/pass.json
python3 -m scripts.flaky.report --runs $W/pass.json --test ok.py --out-dir $W/pass_reports > /dev/null
python3 -c "import json;print('::add-task-context CANNOT_REPRODUCE_TEXT::' + str('upper bound' in json.load(open('$W/pass.json'))['summary']['statement']))"
python3 -m scripts.flaky.rerun --cwd $W/pass --cmd "no-such-runner-xyz tests" --runs 5 --out $W/env.json
python3 -m scripts.flaky.report --runs $W/env.json --test ok.py --out-dir $W/env_reports | tail -1 > $W/env_status.json
echo "::add-task-context ENV_STATUS::$(python3 -c "import json;d=json.load(open('$W/env_status.json'));print(d['status'], d['signal'])")"
# order pollution: file b leaves state that makes the target fail
for f in a b c target; do echo ok > $W/pol/$f.t; done; echo POLLUTE > $W/pol/b.t
cat > $W/pol/runner.py <<'PYEOF'
import sys, pathlib
files = sys.argv[1:]
if any("POLLUTE" in pathlib.Path(f).read_text() for f in files[:-1]):
    print("FAILED", files[-1]); sys.exit(1)
print("1 passed")
PYEOF
cd $W/pol && PYTHONPATH=/app python3 -m scripts.flaky.bisect_polluter --cmd "python3 runner.py {files}" --target target.t --candidates a.t b.t c.t --fail-pattern "FAILED target.t" --cwd $W/pol > $W/bisect.out
echo "::add-task-context POLLUTER::$(python3 -c "import json;print(','.join(json.loads(open('$W/bisect.out').read().split('::add-task-context')[0])['polluters']))")"
'''
            step = h.exec_step(pod, "flaky-pipeline", script, env, timeout=180)
            result.steps.append(step)
            bad = markers_ok(result, step, {
                "DETECTED": "pytest", "SLEEP_FINDING": "True", "STATUS": "DONE", "REPORT_HEADLINE": "True",
                "CANNOT_REPRODUCE_TEXT": "True", "ENV_STATUS": "ERROR BLOCKED", "POLLUTER": "b.t",
            }, ["FLAKY_VERDICT::FLAKY", "FLAKY_VERDICT::CANNOT_REPRODUCE", "FLAKY_VERDICT::ENV_BLOCKED"])
            if bad:
                return bad
        return h._pass(result, "detect -> rerun -> classify -> report; CANNOT_REPRODUCE/ENV_BLOCKED wording; polluter bisected to b.t")

    # ── pr_context ───────────────────────────────────────────────────────────────

    def test_pr_context_gh(base_env):
        result = h.TestResult("qa-pr-context-gh")
        env = dict(base_env)
        env["DEFAULT_TRACKER"] = "github"
        with h.pod_fixture("qa-pr-ctx-gh") as pod:
            step = h.exec_step(pod, "pr-context-gh", f'''
mkdir -p /workspace/prctx && cd /app
python3 -m scripts.common.pr_context "<{GH_PR_URL}|link text>" --out /workspace/prctx/ctx.json --diff-dir /workspace/prctx
rc=$?
echo "::add-task-context RC::$rc"
python3 - <<'PYEOF'
import json
d = json.load(open("/workspace/prctx/ctx.json"))
print("::add-task-context KIND::" + d["kind"])
print("::add-task-context TRACKER::" + d["tracker"])
print("::add-task-context HAS_TITLE::" + str(bool(d["pr"]["title"])))
print("::add-task-context HAS_DIFF::" + str(bool(d["diff_path"]) and len(open(d["diff_path"]).read()) > 0))
print("::add-task-context FILES::" + str(len(d["changed_files"]) > 0))
print("::add-task-context ISSUES::" + str(len(d["issues"])))
PYEOF
''', env, timeout=120)
            result.steps.append(step)
            bad = markers_ok(result, step, {"RC": "0", "KIND": "pr", "TRACKER": "github", "HAS_TITLE": "True", "HAS_DIFF": "True", "FILES": "True"})
            if bad:
                return bad
        return h._pass(result, f"GitHub PR context fetched via gh (title, diff, changed files): {GH_PR_URL}")

    def test_pr_context_bb(base_env):
        result = h.TestResult("qa-pr-context-bb")
        env = dict(base_env)
        has_creds = bool(env.get("BITBUCKET_USERNAME") and (env.get("BITBUCKET_TOKEN") or env.get("BITBUCKET_APP_PASSWORD")))
        url = BB_PR_URL or BB_PLACEHOLDER_URL
        with h.pod_fixture("qa-pr-ctx-bb") as pod:
            step = h.exec_step(pod, "pr-context-bb", f'''
mkdir -p /workspace/prctx && cd /app
python3 -m scripts.common.pr_context "{url}" --out /workspace/prctx/bb.json --diff-dir /workspace/prctx > /workspace/prctx/out.txt 2>/dev/null
echo "::add-task-context RC::$?"
python3 - <<'PYEOF'
import json, pathlib
if pathlib.Path("/workspace/prctx/bb.json").exists():
    d = json.load(open("/workspace/prctx/bb.json"))
    print("::add-task-context TRACKER::" + d["tracker"])
    print("::add-task-context HAS_TITLE::" + str(bool(d["pr"]["title"])))
    print("::add-task-context HAS_DIFF::" + str(bool(d["diff_path"])))
else:
    last = pathlib.Path("/workspace/prctx/out.txt").read_text().strip().splitlines()[-1]
    print("::add-task-context ERROR::" + json.loads(last)["error"])
PYEOF
''', env, timeout=120)
            result.steps.append(step)
            if BB_PR_URL and has_creds:
                bad = markers_ok(result, step, {"RC": "0", "TRACKER": "bitbucket", "HAS_TITLE": "True", "HAS_DIFF": "True"})
                note = "Bitbucket PR fetched over REST (no acli)"
            elif has_creds:
                bad = markers_ok(result, step, {"RC": "2"})
                bad = bad or (None if "not found or not accessible" in step.context.get("ERROR", "") else h._fail(result, step, f"unexpected error: {step.context.get('ERROR')}"))
                note = "no QA_TEST_BB_PR_URL: a non-existent PR is reported as not found (real REST call), nothing invented"
            else:
                bad = markers_ok(result, step, {"RC": "2"})
                bad = bad or (None if "BITBUCKET_USERNAME" in step.context.get("ERROR", "") else h._fail(result, step, f"error does not name the missing variables: {step.context.get('ERROR')}"))
                note = "no Bitbucket credentials in the secret: the BLOCKED error names the missing variables"
            if bad:
                return bad
        return h._pass(result, note)

    # ── QA verify / regression (URL tier) ────────────────────────────────────────

    def _ensure_browser(pod, env):
        """Images built before the Dockerfile fix have no Chromium for the `agent` user; install it for the test pod only."""
        h.exec_step(pod, "browser", 'ls "${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"/chromium* >/dev/null 2>&1 || python3 -m playwright install chromium', env, timeout=600)

    def _fixture_setup(pod, env):
        _ensure_browser(pod, env)
        stage(pod, "/workspace/qa/app.py", FIXTURE_APP, env)
        stage(pod, "/workspace/qa/plan.json", json.dumps(PLAN), env)
        stage(pod, "/workspace/qa/api-tests.sh", API_TESTS, env)
        stage(pod, "/workspace/qa/ui-spec.json", json.dumps(UI_SPEC), env)
        stage(pod, "/workspace/qa/run-all.sh", RUN_ALL, env)

    def test_qa_verify_url(base_env):
        result = h.TestResult("qa-verify-url")
        env = dict(base_env)
        with h.pod_fixture("qa-verify-url") as pod:
            _fixture_setup(pod, env)
            step = h.exec_step(pod, "verify", r'''
cd /app; Q=/workspace/qa; rm -rf $Q/out $Q/reports
cleanup() { kill $APP 2>/dev/null; }
FEATURE=1 python3 $Q/app.py 18081 2>$Q/app.log & APP=$!
trap cleanup EXIT
python3 -m scripts.qa.model $Q/plan.json --url http://127.0.0.1:18081 --allow-mutations > $Q/fitness.json; echo "::add-task-context FITNESS_RC::$?"
# one wrapper around the API script and the UI runner: the environment starts once and both write the same results file
python3 -m scripts.qa.env_manager run --kind url --url http://127.0.0.1:18081 --side candidate --out-dir $Q/out -- bash $Q/run-all.sh
python3 -m scripts.qa.report --plan $Q/plan.json --candidate $Q/out/candidate/results.jsonl --target-url http://127.0.0.1:18081 --out-dir $Q/reports | tail -1 > $Q/status.json
python3 - <<'PYEOF'
import json, glob
Q = "/workspace/qa"
status = json.load(open(Q + "/status.json"))
print("::add-task-context STATUS::" + status["status"])
print("::add-task-context READINESS::" + status["readiness"])
print("::add-task-context EXECUTED::" + str(status["executed"]))
rows = [json.loads(l) for l in open(Q + "/out/candidate/results.jsonl")]
print("::add-task-context MUTATION_BLOCKED::" + str(sum(1 for r in rows if r["outcome"] == "blocked" and "mutating call POST blocked" in r["message"])))
print("::add-task-context SMOKE_RECORDED::" + str(any(r["case"] == "SMOKE1" and r["outcome"] == "pass" for r in rows)))
res = json.load(open(Q + "/reports/result.json"))
print("::add-task-context AC_COUNT::" + str(len(res["ac_status"])))
print("::add-task-context EVIDENCE_PNG::" + str(len(glob.glob(Q + "/reports/evidence/*.png")) >= 1))
print("::add-task-context EVIDENCE_TXT::" + str(len(glob.glob(Q + "/reports/evidence/*.txt")) > 0))
md = open(Q + "/reports/report.md").read()
print("::add-task-context LINKS_RELATIVE::" + str("](evidence/" in md and "/workspace" not in md))
print("::add-task-context SMOKE_NOT_COUNTED::" + str("Additional checks (not part of the plan" in md and "of 5 planned" in md))
seen = open(Q + "/out/candidate/seen-env.txt").read()
print("::add-task-context PLATFORM_TOKEN_HIDDEN::" + str(seen.startswith("None ") and seen.endswith("http://127.0.0.1:18081")))
PYEOF
python3 -m scripts.qa.env_manager run --kind url --url http://127.0.0.1:1 --ready-timeout 2 --side broken --out-dir $Q/out -- true; echo "::add-task-context BROKEN_RC::$?"
''', env, timeout=240)
            result.steps.append(step)
            bad = markers_ok(result, step, {
                "FITNESS_RC": "0", "EVIDENCE_PNG": "True", "EVIDENCE_TXT": "True", "LINKS_RELATIVE": "True", "STATUS": "DONE",
                "READINESS": "Ready with concerns", "EXECUTED": "4", "MUTATION_BLOCKED": "1", "AC_COUNT": "3", "BROKEN_RC": "3",
                "SMOKE_RECORDED": "True", "SMOKE_NOT_COUNTED": "True", "PLATFORM_TOKEN_HIDDEN": "True",
            })
            if bad:
                return bad
        return h._pass(result, "one wrapper for API + real-Chromium UI; mutation blocked; smoke kept out of the verdict; platform token hidden from the test command; unreachable env -> exit 3")

    def test_qa_regression_url(base_env):
        result = h.TestResult("qa-regression-url")
        env = dict(base_env)
        with h.pod_fixture("qa-regr-url") as pod:
            _fixture_setup(pod, env)
            step = h.exec_step(pod, "regression", r'''
cd /app; Q=/workspace/qa; rm -rf $Q/out $Q/reports
cleanup() { kill $BASE $CAND 2>/dev/null; }
python3 $Q/app.py 18082 2>$Q/base.log & BASE=$!
FEATURE=1 BREAK=1 python3 $Q/app.py 18083 2>$Q/cand.log & CAND=$!
trap cleanup EXIT
for side in "baseline 18082" "candidate 18083"; do
  set -- $side
  python3 -m scripts.qa.env_manager run --kind url --url http://127.0.0.1:$2 --side $1 --out-dir $Q/out -- bash $Q/run-all.sh > /dev/null
done
cp $Q/base.log $Q/out/baseline/logs/env.log; cp $Q/cand.log $Q/out/candidate/logs/env.log
python3 -m scripts.qa.delta --plan $Q/plan.json --baseline $Q/out/baseline/results.jsonl --candidate $Q/out/candidate/results.jsonl   --baseline-log $Q/out/baseline/logs/env.log --candidate-log $Q/out/candidate/logs/env.log --out $Q/delta.json
python3 -m scripts.qa.report --mode regression --plan $Q/plan.json --candidate $Q/out/candidate/results.jsonl --delta $Q/delta.json   --target-url http://127.0.0.1:18083 --baseline-url http://127.0.0.1:18082 --out-dir $Q/reports | tail -1 > $Q/status.json
python3 - <<'PYEOF'
import json
Q = "/workspace/qa"
d = json.load(open(Q + "/delta.json"))
by = {r["case"]: r["delta"] for r in d["cases"]}
for case in ("C1", "C2", "C3", "C4"):
    print(f"::add-task-context {case}::" + by[case])
print("::add-task-context DELTA_STATUS::" + d["status"])
print("::add-task-context NEW_LOG_ERRORS::" + str(len(d["new_log_errors"])))
status = json.load(open(Q + "/status.json"))
print("::add-task-context READINESS::" + status["readiness"])
md = open(Q + "/reports/report.md").read()
print("::add-task-context DELTA_TABLE::" + str("| C1 | pass | fail | REGRESSION |" in md))
PYEOF
# regression mode without a delta must be blocked, and a side that executed nothing must never be CLEAN
python3 -m scripts.qa.report --mode regression --plan $Q/plan.json --candidate $Q/out/candidate/results.jsonl --out-dir $Q/reports2 | tail -1 > $Q/nodelta.json
: > $Q/empty.jsonl
python3 -m scripts.qa.delta --baseline $Q/empty.jsonl --candidate $Q/empty.jsonl --out $Q/empty-delta.json > /dev/null
python3 -c "import json;print('::add-task-context NO_DELTA_STATUS::' + json.load(open('$Q/nodelta.json'))['status']);print('::add-task-context EMPTY_STATUS::' + json.load(open('$Q/empty-delta.json'))['status'])"
''', env, timeout=300)
            result.steps.append(step)
            bad = markers_ok(result, step, {
                "DELTA_STATUS": "REGRESSION", "C1": "REGRESSION", "C2": "FIXED", "C3": "FIXED", "C4": "BOTH_PASS", "NEW_LOG_ERRORS": "1",
                "READINESS": "Not ready", "DELTA_TABLE": "True", "NO_DELTA_STATUS": "ERROR", "EMPTY_STATUS": "BLOCKED",
            })
            if bad:
                return bad
        return h._pass(result, "baseline vs candidate through one wrapper per side: REGRESSION/FIXED/BOTH_PASS, new log error, regression without delta and empty runs -> BLOCKED")

    # ── docker tier ──────────────────────────────────────────────────────────────

    def test_docker_preflight_no_daemon(base_env):
        result = h.TestResult("qa-docker-preflight-no-daemon")
        env = dict(base_env)
        env.pop("DOCKER_HOST", None)
        with h.pod_fixture("qa-nodaemon") as pod:
            step = h.exec_step(pod, "preflight", r'''
cd /app
python3 -m scripts.qa.env_manager preflight --kind docker > /workspace/pf.json; echo "::add-task-context RC::$?"
python3 -c "import json;print('::add-task-context MENTIONS_DIND::' + str('--dind' in json.load(open('/workspace/pf.json'))['message']))"
python3 -m scripts.qa.env_manager run --kind docker --image nginx:alpine --out-dir /workspace/nd -- true; echo "::add-task-context RUN_RC::$?"
python3 -c "import pathlib;print('::add-task-context FAILURE_FILE::' + str(pathlib.Path('/workspace/nd/candidate/env-failure.json').exists()))"
''', env, timeout=120)
            result.steps.append(step)
            bad = markers_ok(result, step, {"RC": "3", "MENTIONS_DIND": "True", "RUN_RC": "3", "FAILURE_FILE": "True"})
            if bad:
                return bad
        return h._pass(result, "no daemon: preflight and run exit 3 (BLOCKED) with the --dind hint")

    def test_qa_regression_docker(base_env):
        result = h.TestResult("qa-regression-docker")
        env = dict(base_env)
        env["DOCKER_HOST"] = "tcp://localhost:2375"
        with dind_pod("qa-regr-docker") as pod:
            stage(pod, "/workspace/qa/docker-tests.sh", r'''
source /app/scripts/qa/helpers.sh
qa_case D1; api_call GET /; check_http 200 "root served"
qa_case D2; api_call GET /definitely-missing; check_http 404 "unknown path is 404"
qa_summary
''', env)
            step = h.exec_step(pod, "docker-regression", r'''
cd /app; Q=/workspace/qa; rm -rf $Q/out
python3 -m scripts.qa.env_manager preflight --kind docker; echo "::add-task-context PREFLIGHT_RC::$?"
for pair in "baseline nginx:1.25-alpine" "candidate nginx:alpine"; do
  set -- $pair
  python3 -m scripts.qa.env_manager run --kind docker --image $2 --side $1 --out-dir $Q/out --ready-timeout 120 -- bash $Q/docker-tests.sh
  echo "::add-task-context RUN_RC_$1::$?"
done
python3 -m scripts.qa.delta --baseline $Q/out/baseline/results.jsonl --candidate $Q/out/candidate/results.jsonl --out $Q/delta.json
echo "::add-task-context LEFTOVER_CONTAINERS::$(docker ps -a --filter name=qa- -q | wc -l)"
python3 -c "import pathlib;print('::add-task-context ENV_LOG::' + str(pathlib.Path('$Q/out/candidate/logs/env.log').exists()))"
''', env, timeout=600)
            result.steps.append(step)
            bad = markers_ok(result, step, {
                "PREFLIGHT_RC": "0", "RUN_RC_baseline": "0", "RUN_RC_candidate": "0", "LEFTOVER_CONTAINERS": "0", "ENV_LOG": "True",
                "QA_DELTA_STATUS": "CLEAN",
            })
            if bad:
                return bad
        return h._pass(result, "real docker:dind sidecar: image started with auto-detected port, tests ran, containers removed, delta CLEAN")

    def test_skill_dind_real(base_env):
        result = h.TestResult("skill-dind-real")
        env = dict(base_env)
        env.update({"WORKSPACE_DIR": "/workspace/skill_dind_real", "DEFAULT_TRACKER": "github", "DOCKER_HOST": "tcp://localhost:2375",
                    "RAW_ARGS": "qa-regression --repo https://github.com/bhatti/todo-sample.git --branch main --dind"})
        for k in ("SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK"):
            env.pop(k, None)
        with dind_pod("skill-dind-real") as pod:
            step = h.exec_step(pod, "run-skill-dind", r'''
mkdir -p $WORKSPACE_DIR/logs
python3 -m scripts.skill.run_skill 2>&1 | tail -25
echo "::add-task-context DOCKER_INFO::$(docker info --format '{{.ServerVersion}}' 2>&1 | head -1)"
# The model's Bash inherits only the whitelisted env: docker must work with exactly that environment.
python3 - <<'PYEOF'
import subprocess
from scripts.common.claude_runner import build_claude_env
r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], env=build_claude_env(), capture_output=True, text=True)
print("::add-task-context CLAUDE_ENV_DOCKER::" + ((r.stdout or r.stderr).strip().splitlines() or ["(none)"])[0])
PYEOF
''', env, timeout=300)
            result.steps.append(step)
            bad = markers_ok(result, step, {"SKILL": "qa-regression", "BRANCH": "main", "DOCKER_ENABLED": "true"})
            if bad:
                return bad
            if "BLOCKED" in step.stdout:
                return h._fail(result, step, "blocked even though a docker:dind sidecar is running")
            if not step.context.get("DOCKER_INFO") or "Cannot connect" in step.context["DOCKER_INFO"]:
                return h._fail(result, step, f"docker not usable from the main container: {step.context.get('DOCKER_INFO')}")
            claude_docker = step.context.get("CLAUDE_ENV_DOCKER", "")
            if not claude_docker or "Cannot connect" in claude_docker or "docker.sock" in claude_docker:
                return h._fail(result, step, f"docker not usable with the environment Claude receives: {claude_docker}")
        return h._pass(result, "real docker:dind sidecar: preflight passed, DOCKER_ENABLED marker set, docker CLI works from the skill container")

    def test_qa_worktree_env(base_env):
        result = h.TestResult("qa-worktree-env")
        env = dict(base_env)
        with h.pod_fixture("qa-worktree") as pod:
            stage(pod, "/workspace/qa_wt/repo/app.py", FIXTURE_APP, env)
            stage(pod, "/workspace/qa_wt/tests.sh", "source /app/scripts/qa/helpers.sh\nqa_case W1; api_call GET /api/health; check_http 200 health\nqa_summary", env)
            step = h.exec_step(pod, "worktree", r'''
cd /workspace/qa_wt/repo && git init -q . && git add app.py && git -c user.email=qa@example.com -c user.name=qa commit -qm v1
cd /app; Q=/workspace/qa_wt
python3 -m scripts.qa.env_manager run --kind worktree --ref HEAD --repo-dir $Q/repo --start-cmd "python3 app.py {port}" --env QA_LEAK=1 \
  --side baseline --out-dir $Q/out -- bash $Q/tests.sh > /dev/null
echo "::add-task-context RUN_RC::$?"
python3 -m scripts.qa.env_manager run --kind worktree --ref HEAD --repo-dir $Q/repo --start-cmd "python3 -c 'raise SystemExit(3)'" \
  --side broken --out-dir $Q/out --ready-timeout 20 -- true > /dev/null 2>&1
echo "::add-task-context BROKEN_RC::$?"
python3 - <<'PYEOF'
import json, os, subprocess
Q = "/workspace/qa_wt"
rows = [json.loads(l) for l in open(Q + "/out/baseline/results.jsonl")]
print("::add-task-context W1::" + rows[0]["outcome"])
wt = subprocess.run(["git", "-C", Q + "/repo", "worktree", "list"], capture_output=True, text=True).stdout.strip().splitlines()
print("::add-task-context WORKTREES_LEFT::" + str(len(wt) - 1))
alive = [p for p in os.listdir("/proc") if p.isdigit() and open(f"/proc/{p}/cmdline", "rb").read().split(b"\0")[:2] == [b"python3", b"app.py"]]
print("::add-task-context APP_PROCESSES_LEFT::" + str(len(alive)))
log = open(Q + "/out/baseline/logs/app.log").read()
print("::add-task-context APP_LOG_REDACTED::" + str("abcdefghijkl123" not in log and "<redacted>" in log))
print("::add-task-context APP_LOG_HEAD::" + log[:160].replace("\n", " | "))
fail = json.load(open(Q + "/out/broken/env-failure.json"))
print("::add-task-context BROKEN_ERROR::" + fail["error"])
PYEOF
''', env, timeout=180)
            result.steps.append(step)
            bad = markers_ok(result, step, {"RUN_RC": "0", "BROKEN_RC": "3", "W1": "pass", "WORKTREES_LEFT": "0", "APP_PROCESSES_LEFT": "0", "APP_LOG_REDACTED": "True"})
            if bad:
                return bad
            if "exited before" not in step.context.get("BROKEN_ERROR", ""):
                return h._fail(result, step, f"a crashing app must be reported as exited before ready: {step.context.get('BROKEN_ERROR')}")
        return h._pass(result, "git worktree tier with real git: app started on $PORT, tests ran, worktree + process removed; crashing app -> BLOCKED")

    # ── skills load in the pod ───────────────────────────────────────────────────

    def test_skills_load(base_env):
        result = h.TestResult("qa-skills-load")
        env = dict(base_env)
        if not (YGS_REPO / "skills" / "ygs-qa" / "SKILL.md").exists():
            result.message = f"FAILED: local you-got-skills not found at {YGS_REPO} (set YGS_SKILLS_DIR)"
            return result
        with h.pod_fixture("qa-skills-load") as pod:
            h._kubectl("exec", pod, "--", "bash", "-c", "mkdir -p ~/.claude/skills/you-got-skills/skills", timeout=30)
            for name in YGS_SKILLS:
                h._kubectl("cp", str(YGS_REPO / "skills" / name), f"{pod}:/tmp/{name}", timeout=60)
                h._kubectl("exec", pod, "--", "bash", "-c",
                           f"rm -rf ~/.claude/skills/you-got-skills/skills/{name} && cp -r /tmp/{name} ~/.claude/skills/you-got-skills/skills/{name}"
                           f" && ln -sfn ~/.claude/skills/you-got-skills/skills/{name} ~/.claude/skills/{name}", timeout=30)
            step = h.exec_step(pod, "load", r'''
cd /app
python3 - <<'PYEOF'
from scripts.adhoc.run_skill import _load_skill_md
qa = _load_skill_md("ygs-qa") or ""
fl = _load_skill_md("ygs-flaky-test") or ""
print("::add-task-context QA_LOADED::" + str("Regression mode" in qa and "scripts.qa.report" in qa))
print("::add-task-context FLAKY_LOADED::" + str("Rerun with evidence" in fl and "scripts.flaky.rerun" in fl))
import re, importlib
mods = set(re.findall(r"python3 -m (scripts\.[\w.]+)", qa + fl))
missing = [m for m in sorted(mods) if importlib.util.find_spec(m) is None]
print("::add-task-context MODULES_MISSING::" + (",".join(missing) or "none"))
PYEOF
python3 -m scripts.flaky.rerun --help > /dev/null && echo "::add-task-context FLAKY_HELP::ok"
python3 -m scripts.qa.env_manager --help > /dev/null && echo "::add-task-context QA_HELP::ok"
''', env, timeout=60)
            result.steps.append(step)
            bad = markers_ok(result, step, {"QA_LOADED": "True", "FLAKY_LOADED": "True", "MODULES_MISSING": "none", "FLAKY_HELP": "ok", "QA_HELP": "ok"})
            if bad:
                return bad
        return h._pass(result, "local ygs-qa / ygs-flaky-test load through run_skill._load_skill_md and every module they call exists in the pod image")

    return {
        "flaky-pipeline": test_flaky_pipeline,
        "qa-pr-context-gh": test_pr_context_gh,
        "qa-pr-context-bb": test_pr_context_bb,
        "qa-verify-url": test_qa_verify_url,
        "qa-regression-url": test_qa_regression_url,
        "qa-docker-preflight-no-daemon": test_docker_preflight_no_daemon,
        "qa-worktree-env": test_qa_worktree_env,
        "qa-regression-docker": test_qa_regression_docker,
        "skill-dind-real": test_skill_dind_real,
        "qa-skills-load": test_skills_load,
    }
