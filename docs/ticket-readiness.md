# Ticket readiness: tooling behind `ygs-sprint-ready` and `ygs-enrich-ticket`

Purpose: the deterministic half of two you-got-skills skills. The skills are instructions for the model; the scripts here do the parts
that must not depend on model judgement: reading tickets completely and without guessing, computing the verdict and report from the
model's grades, and writing generated sections to a ticket safely. They work for GitHub issues and Jira issues alike.
Related: [QA and flaky-test skills](qa-and-flaky-skills.md), [architecture](architecture.md), the shared definition in
you-got-skills `skills/shared/ticket-readiness.md`.

## Slack / API usage

```
@bot sprint-ready --sprint "Sprint 12" --dry-run              # Jira sprint: report only
@bot gh sprint-ready --milestone "Sprint 12"                  # GitHub: the milestone is the sprint (report only)
@bot jira sprint-ready --sprint "Sprint 12" --apply           # also enrich what an agent can fix
@bot jira sprint-ready --sprint "Sprint 13" --capacity 40     # compare the open load with the team's capacity (story points)
@bot enrich https://github.com/org/repo/issues/42             # preview only
@bot jira enrich PROJ-123 --apply                             # write the generated sections
@bot gh enrich 42 --repo org/repo                             # a bare GitHub number needs --repo (or GH_ORG and GH_REPO)
```
The tracker is taken from the `gh` / `jira` word, a URL, or the organisation default; the whole message is scanned, so a Jira sprint literally named "GitHub migration" should be run as `jira sprint-ready ...`. These are shortcut routes (`slack-routes.json`, field `args_prefix`) for the generic `skill` job, so `@bot skill ygs-sprint-ready ...` is the same thing. They run the `ai-skill` workflow: the `run` task (the model follows the skill and runs the tools in this image), then `post`, then `notify-error` on failure. No separate workflow is needed: the guarantee that the tools ran is checked by the job, not by the workflow shape (see Reporting).
Nothing is written without `--apply`; a headless run without it previews and says what it would write. Story points are never written. Known limits: Jira offers no compare-and-swap, so the description is re-read just before the write and the write is refused if the section appeared or vanished meanwhile, but an unrelated edit made in that instant can still be overwritten; the GitHub Projects points lookup is opt-in (`GH_PROJECT_NUMBER`), costs one GraphQL call per issue and has been exercised against recorded response shapes only.

## Sprint load: points by lane, carry-over, rollover

For a sprint or milestone scope `fetch` also reads the finished tickets (graded only with `--include-done`) and computes, once, the numbers every report shows (`load` in the fetch output and in `result.json`). The lane model lives in `scripts.common.sprint_load`, shared with the standup, so both always agree.

| Lane | Meaning |
|------|---------|
| pending | not started, or blocked |
| in progress | started, not in review |
| review | status name matches review, code complete, QA, verif, testing |
| done | the workflow's done category (GitHub: closed) |

The status category decides pending / in progress / done and the name splits review out of in progress. A project with other names lists them once: `lanes:` in `.ygs/readiness.yml` (also searched in `$CODEBASE_DIR/.ygs/`) or `SPRINT_LANES` as JSON (`{"review": ["QA Ready"]}`); a malformed value is an error, never ignored. Sub-tasks are left out of the totals (their points live on the parent) and tickets without points are named per lane, including done ones. Flags: unpointed, off-scale / 21 and above, over capacity (`--capacity N`, a number above 0, against pending + in progress + review), carry-over share above `carry_over_share` (default 0.3, a heuristic), chronic carry-over (not done after two or more earlier sprints), carried but not started, carried in flight, hidden rollover (unfinished in the active sprint of the same series, absent from a future target). Jira carry-over comes from the Sprint field history; GitHub has no sprint history, so the report says it was not computed. `fetch --check` verifies the credentials and exits, so a sprint-sized run does not fail halfway.

Questions in `grades.json` may carry a recommended answer (`{question, recommend, because}`); `decisions_made` lists answers used because nobody answered (rendered as "Decisions made for you — confirm"); `still_needed` (`landed`, `duplicate_of`, `spec_drift`) puts a ticket on the engineers' action list.

## Reporting

`scripts.tickets.readiness` writes four files into `reports/`, which the job collects as artifacts:

| File | For | Content |
|------|-----|---------|
| `report.md` / `report.html` | people (the job's "View reports" link) | scoreboard, the sprint load (lane table, capacity, carry-over, hidden rollover, decisions), a Previews section with the acceptance criteria / plan text the enrich skill generated, one card per ticket that needs attention (checklist, quoted evidence, clarifying questions, paste-ready AC starter), ready list, sub-tasks, patterns, action items by owner; ticket ids link to the tracker |
| `result.json` | machines | `verdict`, `summary` counts, one row per ticket (status, gaps, notes, `auto_enrichable`), `meta` |
| `slack.md` | the Slack message | a short mrkdwn summary: verdict line, status counts with ticket ids, who must do what, the most common gaps, one line per ticket needing attention (capped at 15, the rest counted); ticket text is made safe for Slack (no mentions or link markup) |

The `post` task sends `reports/slack.md` when it exists (any tool may provide one), otherwise the converted `report.md`, followed by the link to the job's reports. The job also enforces that the tooling really ran: `ygs-sprint-ready` and `ygs-enrich-ticket` fail the job (BLOCKED) when the model finishes without `reports/result.json` written by `readiness`, and the tool's `result.json` is never overwritten by the job's own status line.

## Modules

| Module | What it does |
|--------|--------------|
| `scripts.tickets.fetch` | Tickets by refs, `--sprint`, `--label`, `--milestone` or `--jql` as one normalized JSON shape (id, status and category, assignee, labels, parent, sub-task, blockers, in-flight, story points `{value, source, known}`, the **complete** description, environment and comments (all pages, no cap; incomplete comments make the ticket unreadable), text signals, plan present). With `--out` every ticket is also written as `tickets/<id>.txt` next to the JSON (real line breaks, nothing cut, parent text for sub-tasks): the file the model reads, because a file reader may cut the very long single lines of the JSON. Unreadable tickets stay in the output as `UNREADABLE`; points that cannot be read are `known: false`, never "unset". Exit 3 on credentials, failed searches, no matches |
| `scripts.tickets.readiness` | Statuses, scoreboard, per-ticket cards, patterns, action items, `report.md` / `report.html` / `result.json` / `slack.md` and the final status line from `tickets.json` + the model's `grades.json` (+ `actions.json`, `.ygs/readiness.yml`). A Missing AC grade is raised to Partial when the text states behavior; an ungraded ticket is never ready |
| `scripts.tickets.write` | Append or (`--force`) replace one generated section; dry run by default; the section is one ADF `panel` ending with a visible "Generated by you-got-skills" footer (Jira) or one marked block (GitHub), so replacing it cannot touch text around it, and a panel or heading a person wrote with the same text is never replaced; refuses finished / In Review / blocked tickets and sub-tasks, ACs while points are unset, a second plan, marker text in the content, bodies over GitHub's limit; re-reads before writing and compares afterwards (rest of the ticket reads the same, the section reads as rendered); a rejected write falls back to a comment, an accepted-but-unverifiable one is `written_unverified` (no comment); comments are never retried after a timeout or server error; redaction (reported) |
| `scripts.tickets.load` / `load_report` / `slack_text` | Sprint load: per-lane totals, carry-over, hidden rollover and the flags (`load`); their markdown / Slack rendering (`load_report`); Slack-safe ticket text (`slack_text`). `readiness --capacity N` compares the open load with capacity; `fetch --check` verifies credentials and exits |
| `scripts.common.sprint_load` | The one lane model (pending / in progress / review / done) and point totals, shared with `scripts.standup.points` (the standup's lanes and tables) |
| `scripts.common.adf` | Atlassian Document Format to readable text (headings, lists, task and decision lists, tables, quotes, link URLs, status, dates, emoji, cards, media, extensions, panels; nothing that carries words is dropped) and markdown to ADF (inline bold/italic/code/links, nested lists, task lists, quotes; tables become code blocks); panel-section helpers |
| `scripts.common.jira_api` | `search_issues_all` (pagination), `fetch_issue`, `list_fields`, `list_comments`, `set_description`, `add_comment_markdown`: explicit `JiraError`, retry on 429/5xx with `Retry-After` (`scripts.common.http_retry`) |

## Environment

`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT` (default project for `--sprint`); `GH_ORG` + `GH_REPO` (or `--repo`);
`JIRA_STORY_POINT_FIELDS` (comma separated field names or ids; default: any field whose name starts with "Story point");
`GH_PROJECT_NUMBER` (read points from a Projects number field; labels `sp:3`, `points: 5`, `estimate/8` otherwise). Optional per-project
settings live in `.ygs/readiness.yml` (`points_scale`, `variants`, `no_enrich_statuses`, `lanes`, `carry_over_share`); `SPRINT_LANES` (JSON) overrides lanes for the standup too. `PYTHONPATH` is forwarded to the model's shell so
`python3 -m scripts.*` works from the job's `/workspace`, and `YGS_HEADLESS=1` tells the skills that nobody can answer a question (they preview instead of asking).

## Safety

Ticket text is untrusted: it is graded, never executed, and secrets in it are redacted from reports and from text written back. The write
path never sends plain text over a Jira description (Atlassian Document Format), never overwrites a GitHub body, and verifies every write.

## Test pyramid

```
python3 -m pytest tests/test_adf.py tests/test_jira_api_tickets.py tests/test_tickets_*.py tests/integration/test_tickets_pipeline.py   # unit + integration (fake Jira server, fake gh)
PYTHONPATH=. python3 tests/test_pod_functional.py --tests tickets-pipeline,tickets-gh-readonly,tickets-model-shell                        # pods
```
Scenarios for the LLM-driven parts: `tests/skill_evals/ygs-sprint-ready.md`, `ygs-enrich-ticket.md`. E2E: run the Slack commands above on a
throwaway sprint and ticket.
