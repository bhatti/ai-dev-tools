# Configuration Reference

All configuration is via environment variables. Set them in a `.env` file for local development, or in Kubernetes Secrets/ConfigMaps for cluster deployment.

## GitHub Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GH_ORG` | Yes | — | GitHub organization or user name |
| `GH_REPO` | Yes | — | GitHub repository name |
| `GH_TOKEN` | Yes | — | GitHub Personal Access Token (needs `repo` scope) |

## Jira Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `JIRA_PROJECT` | Yes | — | Jira project key (e.g. `PROJ`) |
| `JIRA_EMAIL` | Yes | — | Atlassian account email |
| `JIRA_API_TOKEN` | Yes | — | Jira API token |
| `JIRA_BASE_URL` | Yes | — | Atlassian URL (e.g. `https://org.atlassian.net`) |
| `JIRA_HOST` | Yes | — | Atlassian host (e.g. `org.atlassian.net`) |

## BitBucket Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BITBUCKET_USERNAME` | Yes* | — | BitBucket account username (NOT email). Find at bitbucket.org/account/settings/ |
| `BITBUCKET_WORKSPACE` | Yes* | — | BitBucket workspace slug |
| `BITBUCKET_TOKEN` | Yes* | — | Atlassian HTTP Access Token (`ATATT...`) with repo read+write scopes |
| `BITBUCKET_REPO` | Yes* | — | Default repo (overridden by issue label `repo:<repo>` or `repo:<repo>:<branch>`) |

*Required for Jira/BitBucket workflow only.

## Workflow Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PICKUP_LABEL` | `ai-ready` | Label that triggers automation |
| `INPROGRESS_LABEL` | `ai-in-progress` | Label set while working |
| `PR_OPEN_LABEL` | `ai-pr-open` | Label set when PR is created |
| `NEEDS_HUMAN_LABEL` | `needs-human` | Label set when automation is blocked |
| `MAX_ISSUES` | `5` | Max issues to pick per run |
| `POLL_INTERVAL` | `120` | Seconds between PR status polls |

## AI / Claude Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_MODEL` | `claude-sonnet-4-6` | Default Claude model |
| `MAX_TURNS_PLAN` | `50` | Max claude turns for planning step |
| `MAX_TURNS_IMPLEMENT` | `100` | Max claude turns for implementation step |
| `CLAUDE_EFFORT_LEVEL` | `medium` | Claude effort level (`low`, `medium`, `high`) |

## Bedrock / Anthropic Variables

These are written to `~/.claude/settings.json` by the entrypoint.

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_BEDROCK_BASE_URL` | `http://ai/bedrock` | AWS Bedrock proxy URL |
| `CLAUDE_CODE_USE_BEDROCK` | `1` | Enable Bedrock backend |
| `CLAUDE_CODE_SKIP_BEDROCK_AUTH` | `1` | Skip Bedrock auth (for internal proxies) |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `us.anthropic.claude-opus-4-6-v1` | Opus model ID |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `claude-sonnet-4-6` | Sonnet model ID |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Haiku model ID |

## Infrastructure Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKSPACE_DIR` | `/workspace` | Root directory for artifacts |

## Git Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GIT_USER_NAME` | `AI Agent` | Git commit author name |
| `GIT_USER_EMAIL` | `ai-agent@noreply.local` | Git commit author email |
| `BASE_BRANCH` | `main` | Base branch for GitHub PRs |
| `SSH_PRIVATE_KEY` | — | PEM-encoded SSH private key (alternative to HTTPS token for cloning) |

### SSH Key

Mount an SSH private key to `/secrets/ssh-key` in the container, or set the `SSH_PRIVATE_KEY` environment variable with the raw key contents. Used as a fallback when no HTTPS token is available.

## Repo Routing in Jira (via issue labels)

Add a label `repo:<repo>` or `repo:<repo>:<branch>` to a Jira issue to override the default BitBucket repo. The workspace always comes from the `BITBUCKET_WORKSPACE` env var. Examples:

- `repo:frontend:develop` — clones `{BITBUCKET_WORKSPACE}/frontend`, branches from `develop`
- `repo:backend` — clones `{BITBUCKET_WORKSPACE}/backend`, branches from `main`

## GitHub Branch Override

By default the GitHub pipeline branches from `main`. Set `BASE_BRANCH` to override:

```bash
export BASE_BRANCH=develop
```
