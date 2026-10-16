# QA and flaky-test skills: tooling and how to run them

Purpose: the deterministic half of two you-got-skills skills. The skills (`ygs-qa`, `ygs-flaky-test`) are instructions for the model;
the scripts here do the parts that must not depend on model judgement: fetching PR/issue context, rerunning tests with statistics,
managing environments, classifying deltas and rendering reports. They are language-agnostic and work for GitHub and Bitbucket/Jira.
Related: [architecture](architecture.md), [formicary integration](formicary-integration.md).

## Slack / API usage

```
@bot skill ygs-flaky-test --repo org/repo -- tests/cart.test.js         # explicit test (a bare path or ticket text goes after --)
@bot skill ygs-flaky-test https://github.com/org/repo/issues/42         # ticket with a failure excerpt
@bot skill ygs-qa https://bitbucket.org/ws/repo/pull-requests/12 --url https://staging.example.com
@bot skill ygs-qa <pr-url> --baseline-url https://prod.example.com --url https://pr-12.example.com
@bot skill ygs-qa <pr-url> --baseline-image app:1.4 --image app:pr-12 --dind
```
A GitHub PR/issue URL or Bitbucket PR URL as the first argument names the repo; a bare Jira key becomes the identifier. Container tiers (`--image`, `--compose`) need a Docker daemon: add `--dind`. Without one the run stops with BLOCKED and says so.

## Modules

| Module | What it does |
|--------|--------------|
| `scripts.common.pr_context` | PR + linked issues + acceptance criteria + diff as JSON (`--out`, `--diff-dir`); exit 2 on missing credentials/PR |
| `scripts.flaky.detect_runner` / `identify` | Find the runner and the single-test command; extract the failing test from issue/CI text |
| `scripts.flaky.rerun` | Two-phase rerun; verdict FLAKY / DETERMINISTIC_FAIL / CANNOT_REPRODUCE / ENV_BLOCKED with Wilson 95% CI |
| `scripts.flaky.classify` / `bisect_polluter` / `report` | Cause signatures with `file:line`, order-pollution bisection, report + status line |
| `scripts.qa.model` | Plan validation, coverage and GO/CAUTION/NO-GO fitness |
| `scripts.qa.env_manager` | `run` one command (API script + UI runner together) inside a url/docker/compose/worktree environment with guaranteed teardown, redacted logs, and scrubbed credentials; `preflight`; `cleanup` |
| `scripts/qa/helpers.sh` | curl+jq API checks recording JSON Lines |
| `scripts.qa.ui_runner` | Declarative Playwright cases, same-origin only |
| `scripts.qa.delta` / `report` | Baseline vs candidate classification (REGRESSION / NEW_FAILURES / INCOMPLETE / PRE_EXISTING / FIXED / CLEAN, BLOCKED when a side ran nothing); AC traceability report and readiness. Only planned cases count; evidence is copied, redacted, into `reports/evidence/` |
| `scripts.common.redact`, `env_scrub`, `report_bundle`, `docker_check` | Shared: secret redaction (linear time), platform-credential scrubbing for code under test, escaping/evidence/bundle writing, Docker daemon check |

Safety: PR text is untrusted. The code under test runs without credentials (platform tokens and any credential-looking variable are removed from its environment; the clone's origin URL carries no token; installs go through `python3 -m scripts.common.env_scrub -- <cmd>`); helpers refuse hosts other than `QA_BASE_URL` and mutating methods; the UI runner aborts other origins; reports are escaped and redacted.
Environment: `QA_BASE_URL`, `QA_OUT`, `QA_SIDE`, `QA_ALLOW_MUTATIONS` (set by `env_manager run`); `QA_USERNAME`, `QA_PASSWORD`, `QA_AUTH_HEADER`;
`QA_INSECURE_TLS=1`; `QA_CONSOLE_IGNORE=regex,regex`; `QA_ALLOW_ORIGINS=origin,origin`.
Outcome vocabulary and the "zero executed cases is never clean" rule: you-got-skills `skills/shared/evidence-rules.md`.

## Test pyramid

```bash
# 1. unit (no network, no cluster)
python3 -m pytest tests/test_pr_context.py tests/test_flaky_*.py tests/test_qa_*.py tests/test_run_skill_docker_preflight.py tests/test_ygs_skill_structure.py -q

# 2. pod (real pod, local scripts copied over the image; needs kubectl context docker-desktop)
source ~/.zshrc
KUBE_CONTEXT=docker-desktop PYTHONPATH=. python3 tests/test_pod_functional.py \
  --tests flaky-pipeline,qa-pr-context-gh,qa-pr-context-bb,qa-verify-url,qa-regression-url,qa-worktree-env,qa-skills-load,qa-docker-preflight-no-daemon,skill-dind-flag
KUBE_CONTEXT=docker-desktop PYTHONPATH=. python3 tests/test_pod_functional.py --tests qa-regression-docker,skill-dind-real   # starts docker:dind

# 3. e2e: push the skills first (pods clone you-got-skills main); scripts are fetched from ai-dev-tools main at run time.
#    Rebuild the image only for the Dockerfile change (Playwright browsers now live in /opt/ms-playwright; Chromium is no longer installed at run time)
#    then submit the Slack commands above, or the model-behaviour scenarios in tests/skill_evals/
```
`tests/skill_evals/` holds trigger/negative-trigger prompts and pressure scenarios (with a no-skill control) for the two skills. They need an LLM and are not gating.
