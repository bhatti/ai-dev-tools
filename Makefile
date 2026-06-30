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

.PHONY: build push test test-docker lint clean \
        gh-pick gh-plan gh-implement gh-pr gh-poll gh-learn gh-all \
        jira-pick jira-plan jira-implement jira-pr jira-poll jira-learn jira-all \
        k8s-apply k8s-rbac k8s-delete \
        tag release help

## ── Build & Push ───────────────────────────────────────────────────────────

build:           ## Build Docker image
	docker build -t $(IMAGE):$(TAG) .

push: build      ## Push image to registry
	docker push $(IMAGE):$(TAG)

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

test-docker:     ## Run tests inside Docker
	docker run --rm -v $(PWD):/app -w /app $(IMAGE):$(TAG) \
		sh -c "pip install -e '.[dev]' -q && pytest tests/ -v"

lint:            ## Check code style
	python -m py_compile scripts/**/*.py scripts/common/*.py

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
