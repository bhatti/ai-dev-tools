# Setup Guide

## Environment Variables — No .env File Required

You can pass env vars any of three ways:

**Option 1 — Export in shell (simplest, nothing to accidentally commit)**
```bash
export GH_ORG=bhatti
export GH_REPO=todo-sample
export GH_TOKEN=ghp_your_token_here
export ANTHROPIC_API_KEY=sk-ant-your_key_here
export AI_MODEL=claude-sonnet-4-6
# Then run docker compose or make commands as normal
```

**Option 2 — `.env` file (git-ignored)**
```bash
cp .env.example .env
# Edit .env — it is listed in .gitignore, never committed
```

**Option 3 — Shell env + partial `.env`**  
Put non-secret vars in `.env`, export secrets in your shell. Docker Compose merges both.

The `.env.example` file shows every available variable with safe placeholder values.

---

## GitHub Labels Setup

Create the four required labels in your target repo (one-time setup):

```bash
GH_REPO=bhatti/todo-sample   # change to your org/repo

gh label create "ai-ready"       --color "0075ca" --repo $GH_REPO --description "Ready for AI automation"
gh label create "ai-in-progress" --color "e4e669" --repo $GH_REPO --description "AI is working on this"
gh label create "ai-pr-open"     --color "cfd3d7" --repo $GH_REPO --description "AI PR open"
gh label create "needs-human"    --color "d93f0b" --repo $GH_REPO --description "AI is blocked"
```

Then label any issue with `ai-ready` to trigger the pipeline.

---

## Local Python (no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export WORKSPACE_DIR=./test-workspace
export GH_ORG=bhatti
export GH_REPO=todo-sample
export GH_TOKEN=ghp_your_token_here
export ANTHROPIC_API_KEY=sk-ant-your_key_here

mkdir -p test-workspace

PYTHONPATH=. python -m scripts.gh.issue_picker
PYTHONPATH=. python -m scripts.gh.plan --issue-id 3
PYTHONPATH=. python -m scripts.gh.implement --issue-id 3
PYTHONPATH=. python -m scripts.gh.create_pr --issue-id 3
PYTHONPATH=. python -m scripts.gh.monitor_pr --issue-id 3
```

Exit codes: `0` = success, `1` = error (retryable), `2` = blocked (needs human).

---

## Docker Compose — Step by Step

### Build

```bash
docker build -t ai-dev-tools:local .
```

> First build ~3 min. Rebuilds use cache and take ~10s if only Python files changed.

### Create workspace and secrets dirs

```bash
mkdir -p test-workspace secrets
# Optional SSH key (needed only if GH_TOKEN is not set or you prefer SSH):
# cp ~/.ssh/id_rsa secrets/ssh-key
```

### Run each step

```bash
# Step 1 — Pick issue (finds ai-ready issues, writes issue.json)
docker compose run --rm gh-issue-picker

# Verify output:
cat test-workspace/3/issue.json

# Step 2 — Plan (Claude decomposes the issue)
ISSUE_ID=3 docker compose run --rm gh-plan
cat test-workspace/3/plan.md

# Step 3 — Implement (clone, branch, code, commit, push)
ISSUE_ID=3 docker compose run --rm gh-implement
cat test-workspace/3/impl_result.json

# Step 4 — Create PR
ISSUE_ID=3 docker compose run --rm gh-create-pr
cat test-workspace/3/pr.json

# Step 5 — Monitor PR (polls until merged/closed, then calls learn)
ISSUE_ID=3 docker compose run --rm gh-monitor-pr

# Step 6 — Learn (can run standalone without waiting for monitor)
ISSUE_ID=3 docker compose run --rm gh-learn
cat test-workspace/3/learnings.md
```

### Re-running a failed step

All steps are idempotent — they skip if the output checkpoint already says `DONE`.
Delete the checkpoint file to force a re-run:

```bash
rm test-workspace/3/plan_result.json     # re-plan
rm test-workspace/3/impl_result.json     # re-implement
rm test-workspace/3/pr.json              # re-create PR
```

### Debugging inside the container

```bash
# Drop into a bash shell
docker compose run --rm --entrypoint bash gh-plan

# Inside the container you can run scripts directly:
python -m scripts.gh.plan --issue-id 3
ls /workspace/3/
cat /workspace/3/plan.md

# Or check the log files written by each step:
ls /workspace/3/logs/
cat /workspace/3/logs/plan.log
```

### Stream and save output

```bash
ISSUE_ID=3 docker compose run --rm gh-implement 2>&1 | tee /tmp/implement.log
```

---

## Makefile Shortcuts

```bash
make gh-pick                  # issue picker
make gh-plan ISSUE_ID=3       # plan for issue 3
make gh-implement ISSUE_ID=3
make gh-pr ISSUE_ID=3
make gh-monitor ISSUE_ID=3
make gh-learn ISSUE_ID=3

make gh-all ISSUE_ID=3        # all 6 steps in sequence

make clean                    # remove test-workspace/, __pycache__, .pytest_cache
make test                     # run 44 unit tests locally
```

---

## Publishing the Docker Image

### Automatic via GitHub Actions

Every push to `main` builds and pushes to `ghcr.io/bhatti/ai-dev-tools` automatically. Semver tags also push a versioned image:

```bash
git tag v1.0.0 && git push origin v1.0.0
# Pushes: ghcr.io/bhatti/ai-dev-tools:1.0.0 and :1.0
```

The workflow is in `.github/workflows/build-push.yml`. It requires no additional secrets — it uses the automatic `GITHUB_TOKEN` for GHCR.

### Manual push to GHCR

```bash
echo $GH_TOKEN | docker login ghcr.io -u bhatti --password-stdin

docker build -t ghcr.io/bhatti/ai-dev-tools:latest .
docker push ghcr.io/bhatti/ai-dev-tools:latest

# Or using Makefile:
make build push IMAGE=ghcr.io/bhatti/ai-dev-tools TAG=latest
```

### Manual push to Docker Hub

```bash
docker login   # prompts for Docker Hub username + password/token

docker build -t bhatti/ai-dev-tools:latest .
docker push bhatti/ai-dev-tools:latest

make build push IMAGE=bhatti/ai-dev-tools TAG=v1.0.0
```

### Push to AWS ECR

```bash
AWS_ACCOUNT=123456789012
AWS_REGION=us-east-1
REPO=$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/ai-dev-tools

aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com

docker build -t $REPO:latest .
docker push $REPO:latest
```

### Using a custom image tag in docker-compose

If you've pushed a versioned image and want docker compose to pull it instead of building locally, update the `x-common` section in `docker-compose.yml`:

```yaml
x-common: &common
  image: ghcr.io/bhatti/ai-dev-tools:v1.0.0   # ← add this line
  # build: .                                    # ← comment out build
```

Or override on the command line:
```bash
IMAGE_TAG=v1.0.0 ISSUE_ID=3 docker compose run --rm gh-plan
```

---

## Jira / BitBucket Workflow

Same steps as GitHub, but using `jira-*` compose services and a Jira issue key (e.g. `PROJ-42`):

```bash
export JIRA_PROJECT=PROJ
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=your_jira_api_token
export JIRA_BASE_URL=https://yourorg.atlassian.net
export BITBUCKET_USERNAME=your-username
export BITBUCKET_WORKSPACE=your-workspace
export BITBUCKET_TOKEN=your_bb_app_password
export BITBUCKET_REPO=your-repo

docker compose run --rm jira-issue-picker

ISSUE_ID=PROJ-42 docker compose run --rm jira-plan
ISSUE_ID=PROJ-42 docker compose run --rm jira-implement
ISSUE_ID=PROJ-42 docker compose run --rm jira-create-pr
ISSUE_ID=PROJ-42 docker compose run --rm jira-monitor-pr
ISSUE_ID=PROJ-42 docker compose run --rm jira-learn
```

### Jira repo routing

By default the pipeline uses `BITBUCKET_WORKSPACE`/`BITBUCKET_REPO` from env. To route a specific issue to a different repo, add a label to the Jira issue:

```
repo:myworkspace:my-other-repo          # uses main branch
repo:myworkspace:my-other-repo:develop  # uses develop branch
```

---

## Running Tests

```bash
# Local Python (fast):
source .venv/bin/activate
make test              # 44 tests
make test-cov          # with coverage report

# Inside Docker:
make test-docker
```
