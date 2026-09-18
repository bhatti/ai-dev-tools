---
name: integ-tests
argument-hint: "[--module <name>] [-- extra instructions]"
description: "Run integration tests against a repo, optionally health-checking a background service. Produces HTML/MD/Slack reports with pass/fail summary."
---

# integ-tests — Run integration tests and produce reports

Run the project's integration/unit tests, capture results, and produce structured reports.
When a background service is available (via `SERVICE_PORT` env), health-check it first.

## Phase 0: Service Health Check

If `SERVICE_PORT` is set, the task injected a background service. Check if it is healthy:

```bash
SERVICE_HOST="${SERVICE_HOST:-localhost}"
SERVICE_PORT="${SERVICE_PORT:-}"
if [ -n "$SERVICE_PORT" ]; then
  for path in /api/health /health /ping /; do
    CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 \
      "http://${SERVICE_HOST}:${SERVICE_PORT}${path}" 2>/dev/null || echo "000")
    if echo "$CODE" | grep -qE "^2"; then
      echo "::add-task-context SERVICE_HEALTH::ok"
      break
    fi
  done
  # If loop completes without a 2xx: emit fail
fi
```

Emit `::add-task-context SERVICE_HEALTH::ok` on first HTTP 2xx response.
Emit `::add-task-context SERVICE_HEALTH::fail` if all paths return errors or timeout.
Emit `::add-task-context SERVICE_HEALTH::skipped` if `SERVICE_PORT` is not set.

## Phase 1: Discover Test Runner

Look in the working directory or `$CODEBASE_DIR`. Check in order:

| Signal | Command |
|--------|---------|
| `pytest.ini` / `setup.py` / `pyproject.toml` / `requirements.txt` | `python3 -m pytest tests/ -v --tb=short --no-header --ignore=tests/test_pod_functional.py --ignore=tests/test_functional_workflows.py 2>&1` |
| `go.mod` | `go test ./... 2>&1` |
| `Cargo.toml` | `cargo test 2>&1` |
| `pom.xml` | `mvn test -q 2>&1` |
| `package.json` with `test` script | `npm test 2>&1` |
| `Makefile` with `test` target | `make test 2>&1` |

Stop at first match. If `--module <name>` is in the instructions, run only `tests/test_<name>.py`.
If none found, still proceed to Phase 2 and write a report with "no test runner found".

## Phase 2: Run Tests

Run the discovered command inside `CODEBASE_DIR` (or current directory). Capture:
- stdout + stderr combined
- Exit code (0 = all pass, non-zero = failures)
- Pass/fail counts (grep for `passed`, `failed`, `error`, `ok`, `FAIL`)

Use `timeout 300 <command>` to cap long-running suites. Do **not** abort on test failures — always continue to Phase 3.

## Phase 3: Write Reports

Write `reports/report.md`:

```markdown
# Integration Test Report

## Service Health
<ok | fail | skipped — with HTTP path and code if checked>

## Test Runner
<command used>

## Summary
<X passed, Y failed, Z skipped — or "no test runner found">

## Output
<last 200 lines of test output, truncated if longer>
```

Also write `adhoc_report.md` as Slack mrkdwn: `*Test Results*: X passed, Y failed` with failure list.

Emit context markers:
- `::add-task-context TEST_STATUS::pass` if exit code = 0
- `::add-task-context TEST_STATUS::fail` if exit code ≠ 0 or no test runner found
- `::add-task-context SERVICE_HEALTH::<ok|fail|skipped>`

Terminate with JSON on the last line:
`{"status":"DONE","summary":"X/Y tests passed"}`
Or on error: `{"status":"ERROR","reason":"<explanation>"}`
