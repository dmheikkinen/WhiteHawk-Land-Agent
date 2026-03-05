# Ohio Landman Pipeline — developer shortcuts
# macOS / Linux only.  Windows users: run  .\start.ps1
#
# Usage:
#   make setup    — first-time setup (venv + deps + .env scaffold)
#   make run      — start the web UI at http://127.0.0.1:5000

PYTHON  ?= python3
VENV    ?= .venv
PORT    ?= 5000

_PIP     = $(VENV)/bin/pip
_PY      = $(VENV)/bin/python
_UVCORN  = $(VENV)/bin/uvicorn

# Load .env into Make variables (and export them to sub-processes) if present.
# Values must be simple VAR=value lines — no shell expansions.
-include .env
export

.DEFAULT_GOAL := help

# ── First-time setup ──────────────────────────────────────────────────────────

.PHONY: venv
venv:  ## Create the virtual environment
	$(PYTHON) -m venv $(VENV)

.PHONY: install
install: venv  ## Install all Python dependencies
	$(_PIP) install --upgrade pip --quiet
	$(_PIP) install -r requirements.txt --quiet

.PHONY: setup
setup: install  ## Full first-time setup: venv + install + scaffold .env
	@if [ ! -f .env ]; then \
	  cp .env.example .env; \
	  echo ""; \
	  echo "  ✓ .env created — open it and fill in your API keys, then run: make run"; \
	  echo ""; \
	else \
	  echo "  ✓ .env already exists — running make run next"; \
	fi

# ── Run ───────────────────────────────────────────────────────────────────────

.PHONY: run
run:  ## Start the web UI (reads .env automatically)
	$(_UVCORN) server:app --host 127.0.0.1 --port $(PORT) --reload

# ── Pipeline CLI shortcuts ────────────────────────────────────────────────────

.PHONY: harvest
harvest:  ## Run Phase 1 Harvest (CLI)
	$(_PY) harvest_contacts.py

.PHONY: score
score:  ## Run Phase 2 Score (CLI — uses ./data/candidates.csv)
	$(_PY) score_contacts.py data/candidates.csv

.PHONY: orchestrate
orchestrate:  ## Run Phase 3 Orchestrate (CLI)
	$(_PY) run_ohio_landman_assistant.py \
	  --candidates data/candidates.csv \
	  --target 50

.PHONY: dry-run
dry-run:  ## Validate config across all phases without making API calls
	DRY_RUN=1 $(_PY) harvest_contacts.py
	DRY_RUN=1 $(_PY) score_contacts.py data/candidates.csv
	$(_PY) run_ohio_landman_assistant.py --dry-run

# ── Docker ────────────────────────────────────────────────────────────────────

.PHONY: docker-build
docker-build:  ## Build the Docker image
	docker build -t ohio-landman .

.PHONY: docker-run
docker-run:  ## Run in Docker (reads .env, mounts ./data for persistence)
	docker run --rm \
	  -p $(PORT):$(PORT) \
	  --env-file .env \
	  -v "$$(pwd)/data:/app/data" \
	  ohio-landman

.PHONY: docker-up
docker-up:  ## Start with Docker Compose (detached)
	docker compose up -d --build

.PHONY: docker-down
docker-down:  ## Stop Docker Compose stack
	docker compose down

# ── Utilities ─────────────────────────────────────────────────────────────────

.PHONY: clean
clean:  ## Remove __pycache__ and compiled Python files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name "*.pyo" -delete 2>/dev/null || true

.PHONY: help
help:  ## Show this help message
	@echo ""
	@echo "  Ohio Landman Pipeline — make targets"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
