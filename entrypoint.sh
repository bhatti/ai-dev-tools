#!/bin/bash
# Container entrypoint: generate config files, setup auth, install skills, exec command.
set -euo pipefail

# --------------------------------------------------------------------------
# 1. Configure Jira/Atlassian CLI tools from environment variables
# --------------------------------------------------------------------------
if [ -n "${JIRA_BASE_URL:-}" ]; then
  # jira CLI (ankitpokhrel/jira-cli)
  mkdir -p ~/.config/jira
  export JIRA_API_TOKEN="${JIRA_API_TOKEN:-}"
  export JIRA_AUTH_TYPE="${JIRA_AUTH_TYPE:-bearer}"
  cat > ~/.config/jira/.config.yml <<EOF
installation: cloud
base-url: "${JIRA_BASE_URL:-}"
login: "${JIRA_EMAIL:-}"
project: "${JIRA_PROJECT:-}"
EOF
  echo "Generated ~/.config/jira/.config.yml"

  # acli (Atlassian CLI) — same token, same URL
  mkdir -p ~/.config/acli
  cat > ~/.config/acli/config.json <<EOF
{
  "default_profile": "jira",
  "profiles": {
    "jira": {
      "name": "jira",
      "atlassian_url": "${JIRA_BASE_URL:-}",
      "email": "${JIRA_EMAIL:-}",
      "api_token": "${JIRA_API_TOKEN:-}",
      "defaults": { "project": "${JIRA_PROJECT:-}" }
    },
    "bitbucket": {
      "name": "bitbucket",
      "atlassian_url": "${JIRA_BASE_URL:-}",
      "email": "${BITBUCKET_USERNAME:-${BB_USERNAME:-${JIRA_EMAIL:-}}}",
      "api_token": "${BITBUCKET_TOKEN:-${BB_TOKEN:-${JIRA_API_TOKEN:-}}}",
      "defaults": { "workspace": "${BITBUCKET_WORKSPACE:-${BB_WORKSPACE:-}}" }
    }
  }
}
EOF
  echo "Generated ~/.config/acli/config.json"
fi

# --------------------------------------------------------------------------
# 2. Write ~/.claude/settings.json — always overwrite from env vars
# --------------------------------------------------------------------------
mkdir -p ~/.claude

if [ "${CLAUDE_CODE_USE_BEDROCK:-0}" = "1" ]; then
  cat > ~/.claude/settings.json <<EOF
{
  "claudeCodeLocalNetworkingEnabled": true,
  "apiKeyHelper": "echo '-'",
  "env": {
    "ANTHROPIC_BEDROCK_BASE_URL": "${ANTHROPIC_BEDROCK_BASE_URL:-http://ai/bedrock}",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "${CLAUDE_CODE_SKIP_BEDROCK_AUTH:-1}",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "${ANTHROPIC_DEFAULT_OPUS_MODEL:-us.anthropic.claude-opus-4-6-v1}",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "${ANTHROPIC_DEFAULT_SONNET_MODEL:-us.anthropic.claude-sonnet-4-6}",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "${ANTHROPIC_DEFAULT_HAIKU_MODEL:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"
  },
  "model": "${AI_MODEL:-sonnet}",
  "skipWorkflowUsageWarning": true,
  "permissions": { "allow": ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)"] }
}
EOF
  echo "Wrote ~/.claude/settings.json (Bedrock)"
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  cat > ~/.claude/settings.json <<EOF
{
  "claudeCodeLocalNetworkingEnabled": true,
  "skipWorkflowUsageWarning": true,
  "permissions": { "allow": ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)"] }
}
EOF
  echo "Wrote ~/.claude/settings.json (direct API key)"
else
  echo "ERROR: Set CLAUDE_CODE_USE_BEDROCK=1 or ANTHROPIC_API_KEY" >&2
  exit 1
fi

# --------------------------------------------------------------------------
# 3. GitHub authentication
# --------------------------------------------------------------------------
# gh CLI reads GH_TOKEN / GITHUB_TOKEN natively — no `auth login` needed.
# Fine-grained PATs in particular don't work with `auth login --with-token`
# but work fine when the token is just in the environment.
if [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; then
  echo "GitHub token present — gh CLI will use it via environment"
fi

# --------------------------------------------------------------------------
# 4. SSH key setup (mounted from K8s secret at /secrets/ssh-key)
# --------------------------------------------------------------------------
if [ -f /secrets/ssh-key ]; then
  cp /secrets/ssh-key ~/.ssh/id_rsa
  chmod 600 ~/.ssh/id_rsa
  eval "$(ssh-agent -s)" > /dev/null 2>&1
  ssh-add ~/.ssh/id_rsa 2>/dev/null && echo "SSH key loaded"
elif [ -n "${SSH_PRIVATE_KEY:-}" ]; then
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  echo "${SSH_PRIVATE_KEY}" > ~/.ssh/id_rsa
  chmod 600 ~/.ssh/id_rsa
  eval "$(ssh-agent -s)" > /dev/null 2>&1
  ssh-add ~/.ssh/id_rsa 2>/dev/null && echo "SSH key loaded from env"
fi

# --------------------------------------------------------------------------
# 5. Install you-got-skills (base skills — always fresh per pod)
#    Clone into ~/.claude/skills/you-got-skills; symlink each skill directly
#    under ~/.claude/skills/<name> so Claude Code's Skill tool discovers them.
# --------------------------------------------------------------------------
YGS_SKILLS_BASE="${HOME}/.claude/skills"
YGS_INSTALL_DIR="${YGS_SKILLS_BASE}/you-got-skills"

mkdir -p "${YGS_SKILLS_BASE}"
echo "Installing you-got-skills..."
if git clone --depth 1 https://github.com/bhatti/you-got-skills.git "${YGS_INSTALL_DIR}" 2>&1; then
  for skill_dir in "${YGS_INSTALL_DIR}"/skills/ygs-*; do
    skill_name="$(basename "$skill_dir")"
    ln -snf "$skill_dir" "${YGS_SKILLS_BASE}/${skill_name}"
  done
  skill_count=$(ls -d "${YGS_INSTALL_DIR}"/skills/ygs-* 2>/dev/null | wc -l | tr -d ' ')
  echo "you-got-skills: ${skill_count} skills installed → ${YGS_SKILLS_BASE}/"
else
  echo "WARNING: could not clone you-got-skills — continuing without base skills" >&2
fi

# --------------------------------------------------------------------------
# 6. Apply project-level skill overrides (highest precedence)
#    If CODEBASE_DIR is set, scan <CODEBASE_DIR>/.claude/skills/ for any
#    skill directories and symlink them over the base installs.  Project
#    skills win over ygs defaults — same as /etc/init.d local overrides.
# --------------------------------------------------------------------------
_apply_project_skills() {
  local proj_skills_dir="$1"
  [ -d "$proj_skills_dir" ] || return 0
  local count=0
  for skill_dir in "$proj_skills_dir"/*/; do
    [ -d "$skill_dir" ] || continue
    local skill_name
    skill_name="$(basename "$skill_dir")"
    ln -snf "$(cd "$skill_dir" && pwd)" "${YGS_SKILLS_BASE}/${skill_name}"
    count=$((count + 1))
  done
  [ "$count" -gt 0 ] && echo "Project skills: ${count} override(s) applied from ${proj_skills_dir}"
}

if [ -n "${CODEBASE_DIR:-}" ]; then
  _apply_project_skills "${CODEBASE_DIR}/.claude/skills"
fi

# --------------------------------------------------------------------------
# 7. Execute the requested command
# --------------------------------------------------------------------------
exec "$@"
