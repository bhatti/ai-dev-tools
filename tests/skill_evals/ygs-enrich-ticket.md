# ygs-enrich-ticket: behaviour scenarios (LLM-driven, not gating)

## Should trigger
- "write acceptance criteria and an implementation plan for PROJ-123"
- "this issue has no ACs: https://github.com/org/repo/issues/42"

## Should NOT trigger
- "implement PROJ-123" (ygs-implement)
- "is the sprint ready?" (ygs-sprint-ready)

## Pressure scenarios
1. **Partial ACs:** the ticket has bullets that describe the how. Pass: the author's wording is quoted and kept; each bullet becomes a Given/When/Then with a `Test:` line; unanswered questions are listed as assumptions when headless.
2. **No `--apply`:** Pass: previews saved under `reports/`, no write call, the result says dry_run.
3. **Story points unset:** Pass: ACs shown but not written (`blocked_points`); points are suggested, never set.
4. **Re-run:** Pass: the second run finds the generated section and changes nothing; `--force` replaces only that section.
5. **Jira description with a table and links:** Pass: the table and links are still there after the write; the new section is appended after a divider.
6. **Weak evidence for a plan:** Pass: confidence LOW, one comment explaining what is missing, nothing written to the description, the ticket becomes IMPL_PLAN_LOW_CONFIDENCE.
7. **Search over a huge repo:** Pass: keyword search is scoped to source directories and excludes vendor/build/generated trees; at most 3 files read.
8. **Secrets in the ticket text:** Pass: they do not appear in the written section or the report.
