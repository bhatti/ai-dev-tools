---
name: integ-tests
argument-hint: "[--module <name>] [-- extra instructions]"
description: "Run integration tests for ai-dev-tools scripts. Produces HTML/MD/Slack reports with pass/fail summary."
---

# integ-tests — Run integration tests and produce reports

Run the project's unit/integration tests, capture results, and produce structured reports.

## Instructions

1. **Discover tests**: Look in `tests/` relative to the working directory or `$CODEBASE_DIR`.
   If `--module <name>` is in the instructions, run only `tests/test_<name>.py`.
   Otherwise run all `tests/test_*.py` that don't require network (`test_pod_functional.py`
   and `test_functional_workflows.py` are excluded by default).

2. **Run tests** via Bash:
   ```bash
   python3 -m pytest tests/ -v --tb=short --no-header -x 2>&1
   ```
   Capture the full output including pass/fail summary line.

3. **Parse results**: Extract passed, failed, skipped, error counts from the pytest summary.

4. **Write reports**:
   - `reports/report.md` — Summary line + full test output in a fenced code block.
   - `reports/report.html` — Same content in minimal HTML with `<pre>` block.
   - `adhoc_report.md` — Slack mrkdwn: `*Test Results*: X passed, Y failed` with failure list.

5. **Terminate** with JSON on the last line:
   `{"status":"DONE","summary":"X/Y tests passed"}`
   Or on error: `{"status":"ERROR","reason":"<explanation>"}`
