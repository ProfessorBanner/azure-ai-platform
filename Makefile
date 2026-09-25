SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

TERRAFORM_ENV ?= dev
BOOTSTRAP_DIR := bootstrap
BOOTSTRAP_BACKEND := $(CURDIR)/backend/dev.hcl

.DEFAULT_GOAL := help
.PHONY: help check-tools format fmt-check validate lint typecheck security test check clean

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

check-tools: ## Check required local CLI tools
	@./scripts/check-tools.sh


.PHONY: bootstrap-init bootstrap-plan bootstrap-output bootstrap-state

bootstrap-init:
	terraform -chdir=$(BOOTSTRAP_DIR) init \
		-reconfigure \
		-backend-config=$(BOOTSTRAP_BACKEND)

bootstrap-plan:
	terraform -chdir=$(BOOTSTRAP_DIR) plan \
		-lock-timeout=60s

bootstrap-output:
	terraform -chdir=$(BOOTSTRAP_DIR) output

bootstrap-state:
	terraform -chdir=$(BOOTSTRAP_DIR) state list

format: ## Format Terraform and Python code (modifies files)
	@echo "==> Formatting Terraform..."
	@terraform fmt -recursive
	@if [ -f pyproject.toml ]; then \
		echo "==> Formatting Python..."; \
		uv run ruff format .; \
	fi

fmt-check: ## Verify Terraform and Python code formatting without modifying files
	@echo "==> Checking Terraform formatting..."
	@terraform fmt -check -recursive
	@if [ -f pyproject.toml ]; then \
		echo "==> Checking Python formatting..."; \
		uv run ruff format --check .; \
	fi

validate: ## Validate Terraform configurations across environments and modules
	@set -e; \
	directories=$$(find . -not -path '*/.*' -type f -name "*.tf" -exec dirname {} \; | sort -u); \
	if [ -z "$$directories" ]; then \
		echo "No Terraform configurations found."; \
	else \
		for directory in $$directories; do \
			echo "Validating $$directory"; \
			terraform -chdir=$$directory init -backend=false -input=false > /dev/null; \
			terraform -chdir=$$directory validate; \
		done; \
	fi

lint: ## Run TFLint and Ruff linter checks
	@echo "==> Linting Terraform..."
	@if find . -not -path '*/.*' -type f -name "*.tf" | grep -q .; then \
		tflint --recursive; \
	else \
		echo "No Terraform files found."; \
	fi
	@if [ -f pyproject.toml ]; then \
		echo "==> Linting Python..."; \
		uv run ruff check .; \
	fi

typecheck: ## Run Python static type checking
	@if [ -f pyproject.toml ]; then \
		echo "==> Type checking Python..."; \
		uv run mypy products/hello-databricks/src products/hello-databricks/tests; \
	fi

security: ## Run Trivy vulnerability, misconfiguration, and secret scanning
	@echo "==> Running security checks..."
	@trivy fs \
		--ignorefile .trivyignore.yaml \
		--scanners vuln,secret,misconfig \
		--exit-code 1 \
		--severity HIGH,CRITICAL \
		.

test: ## Run automated pytest suite
	@if [ -f pyproject.toml ]; then \
		echo "==> Running Python tests..."; \
		uv run pytest; \
	else \
		echo "No Python project found."; \
	fi

check: check-tools fmt-check validate lint typecheck security test ## Run all non-destructive checks (CI/CD safe)

clean: ## Clean local Terraform cache and Python build artifacts
	@echo "==> Cleaning local artifacts..."
	@find . -type d -name ".terraform" -exec rm -rf {} +
	@find . -type d -name "__pycache__" -exec rm -rf {} +
	@find . -type d -name ".pytest_cache" -exec rm -rf {} +
	@find . -type d -name ".ruff_cache" -exec rm -rf {} +
