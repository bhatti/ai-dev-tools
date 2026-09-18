# AI Dev Tools — Python 3.12 + Node 22 on Debian bookworm (glibc).
# python:3.12-bookworm as the primary base; Node 22 installed via nodesource so native
# npm packages (esbuild, @swc/core, etc.) work with glibc without any musl shims.
FROM python:3.12-bookworm

# Node 22 via nodesource (official Debian channel)
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
  && mkdir -p /etc/apt/keyrings \
  && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
     | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
  && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
     > /etc/apt/sources.list.d/nodesource.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

# Remaining system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    jq \
    unzip \
    openssh-client \
  && rm -rf /var/lib/apt/lists/*

# Create non-root user — claude refuses --dangerously-skip-permissions as root
RUN useradd -m -u 1000 -s /bin/bash agent

# SSH config — disable strict host checking for automated git ops
RUN mkdir -p /home/agent/.ssh && chmod 700 /home/agent/.ssh \
  && printf 'Host *\n  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n' \
     > /home/agent/.ssh/config \
  && chown -R agent:agent /home/agent/.ssh

# gh CLI (GitHub CLI) — architecture-aware
ARG GH_VERSION=2.62.0
RUN ARCH=$(dpkg --print-architecture) \
  && GH_ARCH=$([ "$ARCH" = "arm64" ] && echo "linux_arm64" || echo "linux_amd64") \
  && curl -fsSL \
    "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_${GH_ARCH}.tar.gz" \
  | tar xz -C /usr/local --strip-components=1 \
      "gh_${GH_VERSION}_${GH_ARCH}/bin/gh" \
  && gh --version

# jira CLI (ankitpokhrel/jira-cli) — architecture-aware
ARG JIRA_CLI_VERSION=1.5.2
RUN ARCH=$(dpkg --print-architecture) \
  && JIRA_ARCH=$([ "$ARCH" = "arm64" ] && echo "linux_arm64" || echo "linux_x86_64") \
  && curl -fsSL \
    "https://github.com/ankitpokhrel/jira-cli/releases/download/v${JIRA_CLI_VERSION}/jira_${JIRA_CLI_VERSION}_${JIRA_ARCH}.tar.gz" \
  | tar xz -C /tmp \
  && mv /tmp/jira_${JIRA_CLI_VERSION}_${JIRA_ARCH}/bin/jira /usr/local/bin/jira \
  && chmod +x /usr/local/bin/jira \
  && jira version

# Claude Code and OpenAI Codex CLI (npm global installs)
RUN npm install -g \
    @anthropic-ai/claude-code \
    @openai/codex \
  && npm cache clean --force \
  && claude --version

# Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Application scripts and project-level skills
COPY scripts/ /app/scripts/
COPY .claude/ /app/.claude/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Workspace and app dirs owned by agent
RUN mkdir -p /workspace && chown agent:agent /workspace
RUN chown -R agent:agent /app

USER agent
WORKDIR /app
ENV PYTHONPATH=/app
ENV HOME=/home/agent

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "--version"]
