# AI Dev Tools — minimal container with all AI coding tools pre-installed
FROM python:3.12-alpine

# System packages: git, node/npm, SSH, utilities
RUN apk add --no-cache \
    bash \
    git \
    jq \
    curl \
    tar \
    unzip \
    openssh-client \
    ca-certificates \
    nodejs \
    npm \
    shadow \
  && rm -rf /var/cache/apk/*

# Create non-root user — claude refuses --dangerously-skip-permissions as root
RUN useradd -m -u 1000 -s /bin/bash agent

# SSH config — disable strict host checking for automated git ops
RUN mkdir -p /home/agent/.ssh && chmod 700 /home/agent/.ssh \
  && printf 'Host *\n  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n' \
     > /home/agent/.ssh/config \
  && chown -R agent:agent /home/agent/.ssh

# gh CLI (GitHub CLI)
ARG GH_VERSION=2.62.0
RUN curl -fsSL \
    "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
  | tar xz -C /usr/local --strip-components=1 \
      "gh_${GH_VERSION}_linux_amd64/bin/gh" \
  && gh --version

# jira CLI (ankitpokhrel/jira-cli)
ARG JIRA_CLI_VERSION=1.5.2
RUN curl -fsSL \
    "https://github.com/ankitpokhrel/jira-cli/releases/download/v${JIRA_CLI_VERSION}/jira_${JIRA_CLI_VERSION}_linux_x86_64.tar.gz" \
  | tar xz -C /tmp \
  && mv /tmp/jira_${JIRA_CLI_VERSION}_linux_x86_64/bin/jira /usr/local/bin/jira \
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

# Application scripts
COPY scripts/ /app/scripts/
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
CMD ["python", "--version"]
