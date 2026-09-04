IMAGE    ?= plexobject/ai-dev-tools
TAG      ?= latest
ISSUE_ID ?= 42

VERSION       := $(shell cat VERSION)
_VER_PARTS    := $(subst ., ,$(VERSION))
_VER_MAJOR    := $(word 1,$(_VER_PARTS))
_VER_MINOR    := $(word 2,$(_VER_PARTS))
_VER_PATCH    := $(word 3,$(_VER_PARTS))
_NEXT_PATCH   := $(shell expr $(_VER_PATCH) + 1)
NEXT_VERSION  := $(_VER_MAJOR).$(_VER_MINOR).$(_NEXT_PATCH)

.PHONY: build docker-build docker-push test functional-test functional-tests-min deploy-workflows \
        test-docker lint clean \
        gh-pick gh-plan gh-implement gh-pr gh-poll gh-learn gh-all \
        jira-pick jira-plan jira-implement jira-pr jira-poll jira-learn jira-all \
        k8s-apply k8s-rbac k8s-delete k8s-crons k8s-gh-pipeline k8s-jira-pipeline \
        tag release help

## ── Build & Push ───────────────────────────────────────────────────────────

# docker-build: build + push multi-arch image (amd64 + arm64).
# If push times out, run 'make docker-push' to retry without rebuilding.
NO_CACHE ?=

docker-build:    ## Build multi-arch image (linux/amd64,linux/arm64) and push. Use NO_CACHE=1 to bypass layer cache.
	docker buildx rm multiarch 2>/dev/null || true
	docker buildx create --name multiarch --use --bootstrap \
	    --driver-opt env.BUILDKIT_STEP_LOG_MAX_SIZE=52428800
	docker buildx build --platform linux/amd64,linux/arm64 \
	    $(if $(NO_CACHE),--no-cache,) \
	    -t $(IMAGE):$(VERSION) \
	    -t $(IMAGE):latest \
	    --push --provenance=false .

docker-push:     ## Re-push already-built image tags (no rebuild)
	docker push $(IMAGE):$(VERSION)
	docker push $(IMAGE):latest

build: docker-build  ## Alias for docker-build

## ── Versioning ──────────────────────────────────────────────────────────────

tag:             ## Tag current VERSION (v$(VERSION)) and push the tag
	@echo "Tagging v$(VERSION)"
	git tag -a "v$(VERSION)" -m "Release v$(VERSION)"
	git push origin "v$(VERSION)"
	@echo "Tagged and pushed v$(VERSION)"

release:         ## Bump patch in VERSION, commit, tag, and push ($(VERSION) → $(NEXT_VERSION))
	@echo "$(NEXT_VERSION)" > VERSION
	git add VERSION
	git commit -m "chore: bump version to $(NEXT_VERSION)"
	git tag -a "v$(NEXT_VERSION)" -m "Release v$(NEXT_VERSION)"
	git push origin HEAD "v$(NEXT_VERSION)"
	@echo "Released v$(NEXT_VERSION)"

## ── Tests ──────────────────────────────────────────────────────────────────

test:            ## Run unit tests (local Python)
	PYTHONPATH=. pytest tests/ -v

test-cov:        ## Run tests with coverage report
	PYTHONPATH=. pytest tests/ -v --cov=scripts --cov-report=term-missing

FORMICARY_URL      ?= https://10.8.97.24.nip.io
FORMICARY_EXAMPLES ?= $(CURDIR)/../formicary/docs/examples
K8S_NODE_SSH       ?= k3s-node
PR_URL             ?=
ARGS               ?=

functional-test: ## Run functional tests against live Formicary (raw — no rebuild/deploy). Set ARGS and PR_URL.
	PYTHONPATH=$(CURDIR) \
	FORMICARY_URL=$(FORMICARY_URL) \
	FORMICARY_TOKEN=$(FORMICARY_TOKEN) \
	PR_URL=$(PR_URL) \
	python3 tests/test_functional_workflows.py $(ARGS)

pod-test:        ## Run default pod functional tests (jira-query, jira-analyze, standup-gather). Requires k8s + ai-dev-credentials secret.
	PYTHONPATH=$(CURDIR) python3 tests/test_pod_functional.py --tests jira-query,jira-analyze,standup-gather $(ARGS)

pod-test-all:    ## Run all pod functional tests. Set ISSUE_ID=PROJ-123 for jira-analyze.
	PYTHONPATH=$(CURDIR) ISSUE_ID=$(ISSUE_ID) python3 tests/test_pod_functional.py --tests all $(ARGS)

deploy-workflows: ## Upload all AI workflow YAMLs and set org configs (requires FORMICARY_TOKEN + SLACK_BOT_TOKEN).
	cd $(FORMICARY_EXAMPLES) && FORMICARY_URL=$(FORMICARY_URL) bash deploy-ai-standup-jira.sh --set-configs
	cd $(FORMICARY_EXAMPLES) && FORMICARY_URL=$(FORMICARY_URL) bash deploy-ai-standup-gh.sh --set-configs
	cd $(FORMICARY_EXAMPLES) && FORMICARY_URL=$(FORMICARY_URL) bash deploy-ai-jira-workflows.sh --set-configs

functional-tests-min: docker-build ## Build, clear k8s cache, deploy workflows, run standup+review+prs tests.
	ssh $(K8S_NODE_SSH) "sudo crictl rmi --prune" 2>/dev/null || echo "[warn] crictl prune skipped"
	$(MAKE) deploy-workflows
	$(MAKE) functional-test \
	  ARGS="--tests standup,standup-post,review,review-post,prs --timeout 1200 --skip-health" \
	  PR_URL=$(PR_URL)

test-docker:     ## Run tests inside Docker
	docker run --rm -v $(PWD):/app -w /app $(IMAGE):$(TAG) \
		sh -c "pip install -e '.[dev]' -q && pytest tests/ -v"

lint:            ## Check code style
	python -m py_compile scripts/**/*.py scripts/common/*.py

install-skills:  ## Clone you-got-skills into ~/.skills for local claude CLI use
	@mkdir -p ~/.skills
	@if [ -d ~/.skills/you-got-skills ]; then \
		echo "  ↺ updating ~/.skills/you-got-skills"; \
		git -C ~/.skills/you-got-skills pull --ff-only; \
	else \
		echo "  ✓ cloning you-got-skills into ~/.skills/you-got-skills"; \
		git clone --depth 1 https://github.com/bhatti/you-got-skills.git ~/.skills/you-got-skills; \
	fi
	@echo "  ✓ skills available at ~/.skills/you-got-skills/skills/"

clean:           ## Remove test workspace, __pycache__, .pytest cache
	rm -rf test-workspace/ .pytest_cache/ __pycache__ scripts/**/__pycache__ scripts/__pycache__
	find . -name "*.pyc" -delete

## ── GitHub workflow (local Docker testing) ─────────────────────────────────

gh-pick:         ## Run GitHub issue picker
	docker compose run --rm gh-issue-picker

gh-plan:         ## Run GitHub plan step for ISSUE_ID
	ISSUE_ID=$(ISSUE_ID) docker compose run --rm gh-plan

gh-implement:    ## Run GitHub implement step for ISSUE_ID
	ISSUE_ID=$(ISSUE_ID) docker compose run --rm gh-implement

gh-pr:           ## Run GitHub create-pr step for ISSUE_ID
	ISSUE_ID=$(ISSUE_ID) docker compose run --rm gh-create-pr

gh-poll:      ## Run GitHub poll-pr for ISSUE_ID
	ISSUE_ID=$(ISSUE_ID) docker compose run --rm gh-poll-pr

gh-learn:        ## Run GitHub learn step for ISSUE_ID
	ISSUE_ID=$(ISSUE_ID) docker compose run --rm gh-learn

gh-all: gh-pick gh-plan gh-implement gh-pr gh-poll gh-learn  ## Run full GH pipeline (all steps)

## ── Jira/BitBucket workflow (local Docker testing) ─────────────────────────

jira-pick:       ## Run Jira issue picker
	docker compose run --rm jira-issue-picker

jira-plan:       ## Run Jira plan step for ISSUE_ID
	ISSUE_ID=$(ISSUE_ID) docker compose run --rm jira-plan

jira-implement:  ## Run Jira implement step for ISSUE_ID
	ISSUE_ID=$(ISSUE_ID) docker compose run --rm jira-implement

jira-pr:         ## Run Jira create-pr step for ISSUE_ID
	ISSUE_ID=$(ISSUE_ID) docker compose run --rm jira-create-pr

jira-poll:    ## Run Jira poll-pr for ISSUE_ID
	ISSUE_ID=$(ISSUE_ID) docker compose run --rm jira-poll-pr

jira-learn:      ## Run Jira learn step for ISSUE_ID
	ISSUE_ID=$(ISSUE_ID) docker compose run --rm jira-learn

jira-all: jira-pick jira-plan jira-implement jira-pr jira-poll jira-learn  ## Run full Jira pipeline (all steps)

## ── Kubernetes ──────────────────────────────────────────────────────────────

k8s-apply:       ## Apply all base K8s resources (namespace, pvc, rbac, secrets example)
	kubectl apply -f k8s/namespace.yaml
	kubectl apply -f k8s/pvc.yaml
	kubectl apply -f k8s/rbac.yaml
	@echo "Reminder: apply k8s/secrets.yaml with your real values"

k8s-rbac:        ## Apply RBAC ServiceAccount + Role for Job creation
	kubectl apply -f k8s/rbac.yaml

k8s-gh-pipeline: ## Launch GitHub pipeline job for ISSUE_ID
	ISSUE_ID=$(ISSUE_ID) envsubst < k8s/gh-pipeline-job.yaml | kubectl apply -f -

k8s-jira-pipeline: ## Launch Jira pipeline job for ISSUE_ID
	ISSUE_ID=$(ISSUE_ID) ISSUE_ID_SAFE=$$(echo $(ISSUE_ID) | tr '/' '-') \
		envsubst < k8s/jira-pipeline-job.yaml | kubectl apply -f -

k8s-crons:       ## Deploy issue picker CronJobs
	kubectl apply -f k8s/gh-issue-picker-cron.yaml
	kubectl apply -f k8s/jira-issue-picker-cron.yaml

help:            ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*##"}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
