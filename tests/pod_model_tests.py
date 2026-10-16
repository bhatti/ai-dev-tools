"""Pod functional tests for the single-source model defaults (scripts/common/models.json).

Run the real scripts in a real pod (local scripts copied over the image), no LLM call:

    PYTHONPATH=. python3 tests/test_pod_functional.py --tests model-default,model-override,model-bad-alias,model-tiers

model-default    no flag, no AI_MODEL      -> SELECTED_MODEL is the JSON default
model-override   --model low / AiModel env -> alias resolved, flag beats env
model-bad-alias  --model <typo>            -> job fails before cloning, message lists the aliases
skill-cli-config run_skill writes the acli profiles (entrypoint.sh does not run in Formicary)
model-tiers      implement-style tier export + settings.json written from the JSON
"""

from __future__ import annotations

from types import ModuleType

from scripts.common.models import resolve_model

MODEL_SCRIPTS = [
    "scripts/common/models.py", "scripts/common/models.json", "scripts/common/claude_runner.py", "scripts/common/config.py",
    "scripts/skill/run_skill.py", "scripts/skill/flags.py", "scripts/adhoc/run_skill.py", "scripts/analyze/run_pr_audit.py",
    "scripts/check_connectivity.py", "scripts/common/atlassian_cli.py", "scripts/common/git_utils.py", "scripts/common/acli_fallback.py", "scripts/common/pr_context.py",
]
# Variables a Formicary job could still carry; none may influence the default.
_CLEAR = ("AI_MODEL", "AI_MODEL_TIER", "AI_MODEL_OVERRIDE", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
          "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "CLAUDE_CODE_USE_BEDROCK")


def build_tests(h: ModuleType) -> dict:
    for rel in MODEL_SCRIPTS:
        if rel not in h._SCRIPTS_TO_COPY:
            h._SCRIPTS_TO_COPY.append(rel)

    def skill_env(base_env, workspace, raw_args, **extra):
        env = {k: v for k, v in base_env.items() if k not in _CLEAR and not k.startswith(("JIRA_", "BITBUCKET_"))}
        env.update({"WORKSPACE_DIR": workspace, "DEFAULT_TRACKER": "github", "RAW_ARGS": raw_args, **extra})
        return env

    # Stops at the Claude call (no credentials): everything before it, including SELECTED_MODEL, is real.
    RUN = "mkdir -p $WORKSPACE_DIR/logs\npython3 -m scripts.skill.run_skill 2>&1 | tail -15\n"

    def test_default(base_env):
        result = h.TestResult("model-default")
        with h.pod_fixture("model-default") as pod:
            step = h.exec_step(pod, "run-default", RUN, skill_env(base_env, "/workspace/model_default", "ask -- say hello"), timeout=240)
            result.steps.append(step)
            err = h._check_values(step, {"SELECTED_MODEL": resolve_model()})
            if err:
                return h._fail(result, step, err)
        return h._pass(result, f"no flag / no AI_MODEL -> models.json default {resolve_model()}")

    def test_override(base_env):
        result = h.TestResult("model-override")
        with h.pod_fixture("model-override") as pod:
            low = h.exec_step(pod, "run-flag", RUN, skill_env(base_env, "/workspace/model_flag", "ask --model low -- say hello"), timeout=240)
            result.steps.append(low)
            err = h._check_values(low, {"SELECTED_MODEL": resolve_model(tier="low")})
            if err:
                return h._fail(result, low, err)
            high = h.exec_step(pod, "run-param-vs-flag", RUN, skill_env(
                base_env, "/workspace/model_param", "ask -- say hello", AI_MODEL_OVERRIDE="high"), timeout=240)
            result.steps.append(high)
            err = h._check_values(high, {"SELECTED_MODEL": resolve_model(tier="high")})
            if err:
                return h._fail(result, high, err)
            both = h.exec_step(pod, "run-flag-beats-param", RUN, skill_env(
                base_env, "/workspace/model_both", "ask --model opus -- say hello", AI_MODEL_OVERRIDE="low"), timeout=240)
            result.steps.append(both)
            err = h._check_values(both, {"SELECTED_MODEL": resolve_model(requested="opus")})
            if err:
                return h._fail(result, both, err)
        return h._pass(result, "--model low, AiModel=high and flag-over-param all resolved through models.json")

    def test_bad_alias(base_env):
        result = h.TestResult("model-bad-alias")
        with h.pod_fixture("model-bad") as pod:
            step = h.exec_step(pod, "run-typo", "mkdir -p $WORKSPACE_DIR/logs\npython3 -m scripts.skill.run_skill 2>&1 | tail -8; echo \"::add-task-context RC::${PIPESTATUS[0]}\"",
                               skill_env(base_env, "/workspace/model_bad", "ask --model opuss -- say hello"), timeout=120)
            result.steps.append(step)
            err = h._check_values(step, {"RC": "1"})
            if err:
                return h._fail(result, step, err)
            if "unknown model 'opuss'" not in step.stdout or "sonnet" not in step.stdout:
                return h._fail(result, step, f"error must name the typo and list the aliases:\n{step.stdout[-400:]}")
            if "SELECTED_TRACKER" in step.stdout or "cloned" in step.stdout:
                return h._fail(result, step, "a bad --model must fail before the clone")
        return h._pass(result, "typo in --model -> exit 1 before cloning, message lists the valid aliases")

    def test_tiers(base_env):
        result = h.TestResult("model-tiers")
        env = {k: v for k, v in base_env.items() if k not in _CLEAR}
        with h.pod_fixture("model-tiers") as pod:
            step = h.exec_step(pod, "tiers", r'''
# the implement task: tier from the plan, model from models.json
for c in low medium high bogus; do
  echo "$c" > /tmp/plan_complexity.txt
  COMPLEXITY=$(cat /tmp/plan_complexity.txt)
  case "$COMPLEXITY" in low|medium|high) ;; *) COMPLEXITY=medium ;; esac
  export AI_MODEL_TIER="$COMPLEXITY"
  echo "::add-task-context TIER_$c::$(python3 -c "from scripts.common.config import load_config; print(load_config()['AI_MODEL'])" 2>&1 | tail -1)"
done
unset AI_MODEL_TIER
export HOME=/tmp/home-models; mkdir -p $HOME/.claude; echo '{"enabledPlugins":{"x":true},"env":{"ANTHROPIC_DEFAULT_OPUS_MODEL":"stale"}}' > $HOME/.claude/settings.json
python3 -c "
from pathlib import Path; from scripts.common.claude_runner import write_bedrock_settings
write_bedrock_settings(Path.home() / '.claude' / 'settings.json')"
python3 - <<'PYEOF'
import json, os
s = json.load(open(os.path.expanduser("~/.claude/settings.json")))
print("::add-task-context SETTINGS_OPUS::" + s["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"])
print("::add-task-context SETTINGS_KEPT::" + str(s.get("enabledPlugins") == {"x": True}))
PYEOF
''', env, timeout=120)
            result.steps.append(step)
            err = h._check_values(step, {
                "TIER_low": resolve_model(tier="low"), "TIER_medium": resolve_model(tier="medium"),
                "TIER_high": resolve_model(tier="high"), "TIER_bogus": resolve_model(tier="medium"),
                "SETTINGS_OPUS": resolve_model(requested="opus"), "SETTINGS_KEPT": "True"})
            if err:
                return h._fail(result, step, err)
        return h._pass(result, "plan complexity -> tier -> model through load_config; settings.json refreshed from models.json, other keys kept")

    def test_cli_config(base_env):
        """Formicary bypasses entrypoint.sh: run_skill must create the acli profiles itself."""
        result = h.TestResult("skill-cli-config")
        env = {k: v for k, v in base_env.items() if not k.startswith(("JIRA_", "BITBUCKET_"))}
        env.update({"HOME": "/tmp/home-cli", "JIRA_BASE_URL": "https://example.invalid", "JIRA_EMAIL": "me@example.com",
                    "JIRA_API_TOKEN": "fake-jira", "BITBUCKET_USERNAME": "me@example.com", "BITBUCKET_TOKEN": "fake-bb",
                    "BITBUCKET_WORKSPACE": "acme", "WORKSPACE_DIR": "/workspace/cli_cfg", "DEFAULT_TRACKER": "jira",
                    "QA_USERNAME": "qa-user", "NPM_TOKEN": "npm-secret", "SKILL_ENV_PASSTHROUGH": "NPM_TOKEN", "UNRELATED_SECRET": "x"})
        with h.pod_fixture("skill-cli-config") as pod:
            step = h.exec_step(pod, "cli-config", r'''
mkdir -p $HOME $WORKSPACE_DIR
python3 - <<'PYEOF'
from scripts.common.config import load_config
from scripts.adhoc.run_skill import _setup_environment
_setup_environment(load_config())
PYEOF
python3 - <<'PYEOF'
from scripts.common.claude_runner import build_claude_env
e = build_claude_env()
print("::add-task-context SHELL_CREDS::" + ",".join(k for k in ("BITBUCKET_TOKEN", "JIRA_API_TOKEN", "QA_USERNAME", "NPM_TOKEN", "UNRELATED_SECRET") if k in e))
PYEOF
echo "::add-task-context ACLI_FILE::$(test -s $HOME/.config/acli/config.json && echo yes || echo no)"
echo "::add-task-context JIRA_FILE::$(test -s $HOME/.config/jira/.config.yml && echo yes || echo no)"
echo "::add-task-context ACLI_MODE::$(stat -c %a $HOME/.config/acli/config.json)"
# the profile must resolve: an unreachable host or bad credentials is fine, "profile not found" is the bug
command -v acli > /dev/null || { echo "::add-task-context ACLI_BINARY::missing"; exit 1; }
python3 - <<'PYEOF'
import subprocess
r = subprocess.run(["acli", "-p", "bitbucket", "-o", "json", "bitbucket", "workspace", "get", "acme"], capture_output=True, text=True, timeout=60)
out = (r.stdout + r.stderr).strip()
print("    acli:", out[:160].replace("\n", " | "))
# fake credentials: reaching the API and being rejected ("API error 401") proves the profile and its token were used
print("::add-task-context PROFILE_USED::" + ("yes" if "API error" in out else "no"))
PYEOF
''', env, timeout=150)
            result.steps.append(step)
            err = h._check_values(step, {"ACLI_FILE": "yes", "JIRA_FILE": "yes", "ACLI_MODE": "600", "PROFILE_USED": "yes",
                                         "SHELL_CREDS": "BITBUCKET_TOKEN,JIRA_API_TOKEN,QA_USERNAME,NPM_TOKEN"})
            if err:
                return h._fail(result, step, err)
        return h._pass(result, "run_skill creates the acli jira+bitbucket profiles (0600); acli used the bitbucket profile to reach the API")

    return {
        "skill-cli-config": test_cli_config,
        "model-default": test_default,
        "model-override": test_override,
        "model-bad-alias": test_bad_alias,
        "model-tiers": test_tiers,
    }
