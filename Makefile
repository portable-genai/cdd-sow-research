# B1 CDD + Source-of-Wealth Agent — developer Makefile.
#
# The default dev/test profile is LOCAL: a WORKING offline stack (SQLite FTS5 +
# deterministic LLM) that runs the whole pipeline with NO Google Cloud SDK and NO
# emulators. Override PROFILE=gcp for the managed stack, or PROFILE=onprem to exercise
# the fail-fast migration placeholders.

PYTHON      ?= python3
PYTHON      := $(if $(wildcard .venv/bin/python),.venv/bin/python,$(PYTHON))
PIP         ?= pip
PROFILE     ?= local
SRC         := src/cdd_sow_research
TESTS       := tests
API_APP     := cdd_sow_research.api.app:app
# Loopback by default: the local profile serves no-auth dev personas, so it must
# not listen on every interface out of the box. Containers/prod bind 0.0.0.0
# themselves (Dockerfile CMD, secure profile); override API_HOST deliberately.
API_HOST    ?= 127.0.0.1
API_PORT    ?= 8090
UI_DIR      := ui
TF_DIR      := infra/terraform

export CDD_PROFILE := $(PROFILE)

.DEFAULT_GOAL := help
.PHONY: test-managed help install install-gcp install-oidc lock fmt lint test test-oidc eval check ui-install ui-check run-api run-ui demo demo-selftest laptop-demo laptop-demo-selftest deploy-env-check deploy-preflight deploy-verify-secrets tf-plan clean

help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install the package + dev tooling (NO GCP SDK — onprem/test profile).
	$(PIP) install -e ".[dev]"

install-gcp: ## Install with the managed-stack extra (google-adk, genai, documentai, ...).
	$(PIP) install -e ".[gcp,dev]"

install-oidc: ## Install with the oidc extra (pyjwt[crypto]) — Mode 6 login flow tests.
	$(PIP) install -e ".[oidc,dev]"

lock: ## Recompile EVERY lockfile from pyproject.toml and restore the tag = commit headers.
	$(PYTHON) scripts/lock.py

fmt: ## Auto-format and auto-fix lint issues.
	ruff format $(SRC) $(TESTS) eval
	ruff check --fix $(SRC) $(TESTS) eval

lint: ## Lint (ruff) and type-check (mypy).
	ruff check $(SRC) $(TESTS)
	ruff format --check $(SRC) $(TESTS)
	mypy $(SRC)

test: ## Run unit + contract tests on the local profile (no GCP SDK required).
	CDD_PROFILE=local pytest -m 'not integration' -q

test-managed: ## Managed round-trip suite against a NAMED deployment (needs live credentials).
	@# Three states, never two: CDD_MANAGED_TEST_PROJECT unset skips (so an offline gate is
	@# unaffected), named-and-reachable runs, named-and-unusable FAILS. A managed suite that
	@# skips when its configuration is wrong reports the same green as one that ran.
	CDD_PROFILE=gcp pytest -m integration -q tests/integration/test_managed_profile_paths.py -rs

test-oidc: ## Run the PyJWT-dependent tests (needs `make install-oidc` first).
	CDD_PROFILE=local pytest -m 'not integration' -q \
		$(TESTS)/unit/test_oidc_auth_flow.py \
		$(TESTS)/unit/test_oidc_session_identity.py \
		$(TESTS)/unit/test_access_token_identity.py \
		$(TESTS)/unit/test_private_key_jwt.py \
		$(TESTS)/unit/test_embed_token.py \
		$(TESTS)/unit/test_embed_broker_integration.py \
		$(TESTS)/unit/test_id_token_subject.py \
		$(TESTS)/unit/test_embed_app_composition.py \
		$(TESTS)/unit/test_citation_continuation.py \
		$(TESTS)/browser/test_identity_harness.py

eval: ## Run the A4 eval gate (sow_groundedness / risk_band / citations / pii_safety).
	$(PYTHON) eval/run_eval.py

portability: ## Execute the bounded offline/profile portability proof.
	PYTHONPATH=src $(PYTHON) scripts/portability_demo.py

plugin: ## Render the Agent Plugins 1.0.0 directory from this repo's own declarations.
	python scripts/render_plugin.py --dest dist/plugin

mcp-serve: ## Serve the governed tool catalog over MCP 2026-07-28 (stdio; needs the [gcp] extra).
	python -m cdd_sow_research.mcp

check: lint test eval portability plugin ## The full offline Python gate (no node, no cloud).

ui-install: ## Install the console's locked node dependencies.
	npm ci --prefix $(UI_DIR)

ui-check: ## The console gate. `assert-hydratable` runs LAST, against the artefact `build` just made.
	npm --prefix $(UI_DIR) run lint
	npm --prefix $(UI_DIR) test
	NEXT_TELEMETRY_DISABLED=1 npm --prefix $(UI_DIR) run build
	npm --prefix $(UI_DIR) run assert-hydratable

run-api: ## Run the FastAPI service (PROFILE=$(PROFILE)).
	uvicorn $(API_APP) --host $(API_HOST) --port $(API_PORT) --reload

run-ui: ## Run the React / Next.js UI (dev server).
	cd $(UI_DIR) && npm install && npm run dev

demo: ## Presenter-controlled SoW walkthrough (auto-starts its own server; needs the demo extra).
	PYTHONPATH=src $(PYTHON) scripts/sow_demo_playwright.py

demo-selftest: ## Headless, unattended run of the walkthrough (CI smoke test; asserts every step).
	HEADLESS=1 DEMO_AUTO=1 PYTHONPATH=src $(PYTHON) scripts/sow_demo_playwright.py

laptop-demo: ## Real standalone UI: capabilities, functional dossier, open export/reload.
	$(PYTHON) scripts/laptop_demo_playwright.py

laptop-demo-selftest: ## Start the isolated stack and verify the laptop UI unattended.
	$(PYTHON) scripts/laptop_demo_playwright.py --headless --no-pause

deploy-env-check: ## Validate draft .env and .env.secrets files without cloud access.
	$(PYTHON) scripts/deployment_env.py validate

deploy-preflight: ## Fail unless every named-production input is real and approved.
	$(PYTHON) scripts/deployment_env.py validate --require-ready

deploy-verify-secrets: ## Bind exact Secret Manager versions to reviewed payload digests.
	$(PYTHON) scripts/deployment_env.py verify-secrets

tf-plan: ## Terraform plan for the deployment infrastructure (region is a deploy-time input).
	$(PYTHON) scripts/deployment_env.py run -- terraform -chdir=$(TF_DIR) init -input=false
	$(PYTHON) scripts/deployment_env.py run -- terraform -chdir=$(TF_DIR) plan

clean: ## Remove caches and build artefacts.
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
