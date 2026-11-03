"""Pod functional tests for pr-audit with and without --reviewers, against the real trackers.

Real scripts in real pods (local scripts copied over the image), real GitHub / Bitbucket (read-only), real model for the
full runs. Names the user types ("CJ", "pat", "JB") must resolve to people through the real workspace directory.

    PYTHONPATH=. KUBE_CONTEXT=docker-desktop python3 tests/test_pod_functional.py \
        --tests pr-audit-reviewers-bb,pr-audit-reviewers-bb-jira-scope,pr-audit-reviewers-gh

    PYTHONPATH=. KUBE_CONTEXT=docker-desktop python3 tests/test_pod_functional.py \
        --tests pr-audit-reviewers-full-bb,pr-audit-no-reviewers-full-bb,pr-audit-reviewers-nomatch-bb

pr-audit-reviewers-bb            fetch: CJ,pat,JB resolve to people, PRs match, aliases are recorded (no Jira scope)
pr-audit-reviewers-bb-jira-scope same, with the Jira board scope active (the case that leaked unfiltered PRs)
pr-audit-reviewers-gh            fetch on GitHub with a login (server-side search)
pr-audit-reviewers-full-bb       whole audit + dry-run PR step: only the requested reviewers reach the report and the knowledge base
pr-audit-no-reviewers-full-bb    same without --reviewers: everyone's learnings, no reviewer profiles, plain listing
pr-audit-reviewers-nomatch-bb    a name nobody has: the audit still runs, says so, and shows everyone's learnings
Env (each test is skipped when its names are not set): PAR_BB_REVIEWERS (names to resolve in your Bitbucket workspace, comma-separated),
PAR_GH_ORG / PAR_GH_REPO / PAR_GH_REVIEWER (a GitHub repo and a login that reviewed PRs in it).
"""

from __future__ import annotations

import os
from types import ModuleType

SCRIPTS = [
    "scripts/analyze/reviewer_scope.py", "scripts/analyze/audit_options.py", "scripts/analyze/kb_writer.py",
    "scripts/analyze/learnings_report.py", "scripts/analyze/skills_report.py", "scripts/analyze/pr_fetcher.py",
    "scripts/analyze/run_pr_audit.py", "scripts/analyze/post_pr_audit.py", "scripts/analyze/create_skill_pr.py",
    "scripts/analyze/plan_skill_updates.py", "scripts/analyze/create_ygs_pr.py", "scripts/common/person_match.py",
    "scripts/common/comment_select.py", "scripts/common/http_retry.py", "scripts/common/report_renderer.py",
    "scripts/common/config.py", "scripts/common/slack_format.py",
]
BB_REVIEWERS = os.environ.get("PAR_BB_REVIEWERS", "")
GH_ORG = os.environ.get("PAR_GH_ORG", "")
GH_REPO = os.environ.get("PAR_GH_REPO", "")
GH_REVIEWER = os.environ.get("PAR_GH_REVIEWER", "")

_FETCH_CHECK = r'''
import json, sys
sys.path.insert(0, "/app")
from scripts.common.config import load_config
from scripts.analyze.pr_fetcher import fetch_prs, build_pr_context
from scripts.analyze import reviewer_scope as rs
cfg = load_config()
cfg["DEFAULT_TRACKER"] = "{tracker}"
cfg["PR_AUDIT_REVIEWERS"] = "{reviewers}"
prs = fetch_prs(cfg, 4)
scope = cfg.get("_reviewer_scope", {{}})
aliases = rs.identities(prs, scope)
print(f"::add-task-context PRS::{{len(prs)}}")
print(f"::add-task-context MATCHED::{{','.join(aliases)}}")
print(f"::add-task-context RESOLVED::{{json.dumps({{k: [p.get('display_name') or p.get('nickname') for p in v] for k, v in scope.items()}})}}")
if not prs:
    print("FAIL: no PRs", file=sys.stderr); sys.exit(1)
bad = [p["number"] for p in prs if not p.get("matched_reviewers")]
if bad:
    print(f"FAIL: PRs without a reviewer match (unfiltered leak): {{bad}}", file=sys.stderr); sys.exit(1)
dup = len(prs) - len({{p["number"] for p in prs}})
if dup:
    print(f"FAIL: {{dup}} duplicate PRs", file=sys.stderr); sys.exit(1)
ctx = build_pr_context(prs, max_chars=60000, priority_authors=tuple("{reviewers}".split(",")))
print(f"::add-task-context CONTEXT_CHARS::{{len(ctx)}}")
'''


def build_tests(h: ModuleType) -> dict:
    for rel in SCRIPTS:
        if rel not in h._SCRIPTS_TO_COPY:
            h._SCRIPTS_TO_COPY.append(rel)

    def bb_env(base_env, ws, jira_scope):
        env = {k: v for k, v in base_env.items() if not k.startswith("GH_")}
        env.update({"DEFAULT_TRACKER": "jira", "WORKSPACE_DIR": ws, "SLACK_BOT_TOKEN": "", "PR_SIGNALS": "off"})
                for k in ("JIRA_BOARDS", "JIRA_SPACE", "PR_AUDIT_FILTER"):
            env[k] = ""
        if jira_scope:  # the org's Jira scope (what the Slack job runs with); the secret does not carry it
            env["JIRA_BOARDS"] = os.environ.get("JIRA_BOARDS", "") or env.get("JIRA_BOARDS", "")
        return env

    def fetch_test(name, tracker, reviewers, env_fn, expect_all):
        def run(base_env):
            result = h.TestResult(name)
            if not reviewers or (tracker == "github" and not (GH_ORG and GH_REPO)):
                result.passed, result.message = True, "SKIPPED — set PAR_BB_REVIEWERS / PAR_GH_ORG, PAR_GH_REPO, PAR_GH_REVIEWER"
                return result
            ws = f"/workspace/{name}"
            env = env_fn(base_env, ws)
            if "jira-scope" in name and not env.get("JIRA_BOARDS"):
                return h._fail(result, h.StepResult(label="env", returncode=1), "JIRA_BOARDS is not set locally: cannot test the Jira scope")
            script = _FETCH_CHECK.format(tracker=tracker, reviewers=reviewers)
            with h.pod_fixture(name) as pod:
                h._kubectl("exec", pod, "--", "bash", "-c", f"cat > /tmp/check.py << 'PYEOF'\n{script}\nPYEOF")
                step = h.exec_step(pod, "fetch", f"mkdir -p {ws}/reports && python3 /tmp/check.py", env, timeout=600)
                result.steps.append(step)
            if not step.ok:
                return h._fail(result, step, f"exit {step.returncode}: {step.stderr[-500:]}")
            missing = [n for n in reviewers.split(",") if n not in step.context.get("MATCHED", "").split(",")] if expect_all else []
            if missing:
                return h._fail(result, step, f"requested reviewers not matched: {missing} (resolved: {step.context.get('RESOLVED')})")
            return h._pass(result, f"prs={step.context.get('PRS')} matched={step.context.get('MATCHED')} resolved={step.context.get('RESOLVED')} "
                                   f"ctx={step.context.get('CONTEXT_CHARS')} elapsed={step.elapsed:.0f}s")
        return run

    def gh_env(base_env, ws):
        env = {k: v for k, v in base_env.items() if not k.startswith(("JIRA_", "BITBUCKET_"))}
        env.update({"GH_ORG": GH_ORG, "GH_REPO": GH_REPO, "DEFAULT_TRACKER": "github", "WORKSPACE_DIR": ws, "PR_SIGNALS": "off"})
        return env

    def full_test(name, message, checks):
        """Audit (model) then the dry-run PR step in one pod; ``checks`` is a python snippet run on the results."""
        def run(base_env):
            result = h.TestResult(name)
            if not BB_REVIEWERS and "zzzqqq" not in message:
                result.passed, result.message = True, "SKIPPED — set PAR_BB_REVIEWERS"
                return result
            if not (base_env.get("CLAUDE_CODE_USE_BEDROCK") == "1" or base_env.get("ANTHROPIC_API_KEY")):
                result.passed, result.message = True, "SKIPPED — no Claude credentials"
                return result
            ws = f"/workspace/{name}"
            env = bb_env(base_env, ws, jira_scope=False)
            env.update({"SLACK_MESSAGE": message, "MAX_TURNS_AUDIT": "30", "N_PRS": "3", "CODEBASE_DIR": f"{ws}/repo",
                        "AI_MODEL": base_env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "us.anthropic.claude-sonnet-4-6"),
                        "SLACK_CHANNEL": "", "SLACK_THREAD_TS": ""})
            with h.pod_fixture(name) as pod:
                audit = h.exec_step(pod, "audit", f"mkdir -p {ws}/reports {ws}/logs && python3 -m scripts.analyze.run_pr_audit --n-prs 3",
                                    env, timeout=1500)
                result.steps.append(audit)
                if not audit.ok:
                    return h._fail(result, audit, f"audit exit {audit.returncode}: {audit.stderr[-500:]}")
                create = h.exec_step(pod, "create-pr-dry-run", "python3 -m scripts.analyze.create_skill_pr", env, timeout=600)
                result.steps.append(create)
                if not create.ok:
                    return h._fail(result, create, f"create_skill_pr exit {create.returncode}: {create.stderr[-500:]}")
                diag = h.exec_step(pod, "diagnose", f"ls -la {ws}/reports | head -40; echo ---; cat {ws}/reports/run_options.json; echo ---; "
                                   f"head -c 1500 {ws}/reports/review_learnings.md; echo ---; ls {ws}/reports/dry_run 2>&1 | head", env, timeout=60)
                result.steps.append(diag)
                h._kubectl("exec", pod, "--", "bash", "-c", f"cat > /tmp/verify.py << 'PYEOF'\nimport json, os, glob\nws = '{ws}'\n{checks}\nPYEOF")
                verify = h.exec_step(pod, "verify", "python3 /tmp/verify.py", env, timeout=60)
                result.steps.append(verify)
            if not verify.ok:
                return h._fail(result, verify, f"{verify.stdout[-600:]} {verify.stderr[-600:]}")
            return h._pass(result, f"{verify.context.get('SUMMARY', '')} elapsed={audit.elapsed + create.elapsed:.0f}s")
        return run

    common = (
        "issues = []\n"
        "R = ws + '/reports'\n"
        "for f in ('pr_audit_report.md', 'pr_audit_report.html', 'review_learnings.md', 'review_learnings.html',\n"
        "          'skill_updates_report.md', 'skill_updates_report.html', 'run_options.json'):\n"
        "    if not os.path.exists(R + '/' + f) or os.path.getsize(R + '/' + f) == 0: issues.append('missing report ' + f)\n"
        "opts = json.load(open(R + '/run_options.json'))\n"
        "md = open(R + '/review_learnings.md').read() if os.path.exists(R + '/review_learnings.md') else ''\n"
        "kb = glob.glob(R + '/dry_run/files/.claude/skills/review-knowledge/**/*.md', recursive=True)\n"
        "profiles = [k for k in kb if '/reviewers/' in k]\n"
        "if not opts.get('dry_run'): issues.append('dry_run not recorded')\n"
        "if os.path.exists(ws + '/pr.json') and json.load(open(ws + '/pr.json')).get('number'): issues.append('a PR was created in dry-run')\n"
    )
    finish = (
        "if issues:\n"
        "    print('ISSUES: ' + '; '.join(issues)); raise SystemExit(1)\n"
        "print('::add-task-context SUMMARY::' + SUMMARY)\n"
    )

    with_reviewers = common + (
        "if sorted(opts['reviewers']) != sorted('%s'.split(',')) and not opts['reviewers']: issues.append('all requested reviewers dropped: ' + str(opts))\n"
        "aliases = opts.get('reviewer_aliases', {})\n"
        "if not aliases: issues.append('no reviewer aliases recorded')\n"
        "if 'Reviewers analyzed' not in md: issues.append('report does not list the analyzed reviewers')\n"
        "allowed = {i.lower() for ids in aliases.values() for i in ids} | {k.lower() for k in aliases}\n"
        "ents = json.load(open(R + '/review_learnings.json')).get('entries', []) if os.path.exists(R + '/review_learnings.json') else []\n"
        "SUMMARY = f'aliases={list(aliases)} entries={len(ents)} kb_files={len(kb)} profiles={len(profiles)}'\n" % BB_REVIEWERS
    ) + finish
    without = common + (
        "if opts['reviewers']: issues.append('reviewers recorded without being requested')\n"
        "if profiles: issues.append('reviewer profiles written without reviewers: ' + str(profiles))\n"
        "SUMMARY = f'kb_files={len(kb)} entries_in_report={md.count(chr(10) + chr(45) + chr(32) + chr(42) + chr(42))}'\n"
    ) + finish
    nomatch = common + (
        "if opts['reviewers']: issues.append('reviewers should be dropped when none match')\n"
        "if opts.get('unmatched_reviewers') != ['zzzqqq']: issues.append('unmatched not recorded: ' + str(opts.get('unmatched_reviewers')))\n"
        "if 'None of the requested reviewers' not in md: issues.append('report does not say the reviewers were not found')\n"
        "SUMMARY = f'fallback ok kb_files={len(kb)}'\n"
    ) + finish

    return {
        "pr-audit-reviewers-bb": fetch_test("pr-audit-reviewers-bb", "jira", BB_REVIEWERS, lambda b, ws: bb_env(b, ws, False), True),
        "pr-audit-reviewers-bb-jira-scope": fetch_test("pr-audit-reviewers-bb-jira-scope", "jira", BB_REVIEWERS, lambda b, ws: bb_env(b, ws, True), True),
        "pr-audit-reviewers-gh": fetch_test("pr-audit-reviewers-gh", "github", GH_REVIEWER, gh_env, True),
        "pr-audit-reviewers-full-bb": full_test("pr-audit-reviewers-full-bb", f"--n-prs 3 --reviewers {BB_REVIEWERS} --dry-run", with_reviewers),
        "pr-audit-no-reviewers-full-bb": full_test("pr-audit-no-reviewers-full-bb", "--n-prs 3 --dry-run", without),
        "pr-audit-reviewers-nomatch-bb": full_test("pr-audit-reviewers-nomatch-bb", "--n-prs 3 --reviewers zzzqqq --dry-run", nomatch),
    }
