.PHONY: help menu install install-dev test lint format format-check typecheck check status sync inventory release audit cleanup verify mirror init conventions \
        docker-build docker-shell docker-push docker-run \
        docker-up docker-down docker-logs docker-ps docker-exec docker-host-shell \
        clean

# Pick the first available Python 3.12+ interpreter. Some hosts ship a `python3`
# that resolves to 3.10 (e.g. MAMP, system Python on older macOS) and won't have
# pytest / mypy installed — overrideable via PYTHON=path/to/bin.
PYTHON ?= $(shell command -v python3.12 || command -v python3.13 || command -v python3)
UV     ?= uv
ARGS   ?=

# Container name used by docker-exec / docker-logs / docker-down. The compose
# service is `gendia`; the default container name follows Compose conventions.
COMPOSE_SERVICE ?= gendia
CONTAINER       ?= gendia-$(COMPOSE_SERVICE)-1

help:                ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-22s %s\n", $$1, $$2}'

menu:                ## Launch the interactive gendia menu (bin/gendia-menu)
	@./bin/gendia-menu

install:             ## Install gendia from source (uv preferred, pip fallback)
	@if command -v $(UV) >/dev/null 2>&1; then \
		$(UV) tool install --upgrade .; \
	else \
		$(PYTHON) -m pip install --upgrade .; \
	fi

install-dev:         ## Install with dev extras for local hacking
	@if command -v $(UV) >/dev/null 2>&1; then \
		$(UV) sync --extra dev; \
	else \
		$(PYTHON) -m pip install --upgrade -e '.[dev]'; \
	fi

test:                ## Run pytest
	$(PYTHON) -m pytest

lint:                ## Run ruff lint
	$(PYTHON) -m ruff check src tests

format:              ## Apply ruff format to src + tests (rewrites files)
	$(PYTHON) -m ruff format src tests

format-check:        ## Check ruff format compliance without writing (mirrors CI)
	$(PYTHON) -m ruff format --check src tests

typecheck:           ## Run mypy
	$(PYTHON) -m mypy src

check: lint format-check typecheck test  ## Run lint + format-check + typecheck + tests (CI gates)

# --- Operations ---------------------------------------------------------------

status:              ## gendia status [ARGS=...]
	gendia status $(ARGS)

sync:                ## gendia sync [ARGS=...]
	gendia sync $(ARGS)

release:             ## REPO=myorg/core VERSION=1.0.1 make release
	@test -n "$(REPO)"    || (echo "REPO=... required";    exit 1)
	@test -n "$(VERSION)" || (echo "VERSION=... required"; exit 1)
	gendia release $(REPO) $(VERSION)

inventory audit cleanup verify mirror init conventions: ## gendia <target> [ARGS=...]
	gendia $@ $(ARGS)

# --- Docker (host-side: build / push / one-shot run) -------------------------

docker-build:        ## Build the gendia Docker image
	docker build -t gendia:latest .

docker-shell:        ## Drop into a one-shot gendia container with config + .env mounted
	docker run --rm -it \
		--env-file "$(HOME)/.config/gendia/.env" \
		-v "$(HOME)/.config/gendia:/config:ro" \
		-v "$(HOME)/.ssh:/home/gendia/.ssh:ro" \
		-v "$(PWD):/work" \
		--entrypoint bash gendia:latest

docker-run:          ## VERB=sync make docker-run — run any gendia verb in a one-shot container
	@test -n "$(VERB)" || (echo "VERB=... required (e.g. VERB=sync)"; exit 1)
	docker run --rm \
		--env-file "$(HOME)/.config/gendia/.env" \
		-v "$(HOME)/.config/gendia:/config:ro" \
		-v "$(HOME)/.ssh:/home/gendia/.ssh:ro" \
		-v "$(PWD):/work" \
		gendia:latest $(VERB) $(ARGS)

docker-push:         ## REGISTRY=ghcr.io/your-org make docker-push
	@test -n "$(REGISTRY)" || (echo "REGISTRY=... required"; exit 1)
	docker tag gendia:latest $(REGISTRY)/gendia:latest
	docker push $(REGISTRY)/gendia:latest

# --- Docker Compose (long-lived service: up / exec / down) -------------------

docker-up:           ## Start the gendia compose service in the background
	docker compose up -d

docker-down:         ## Stop the gendia compose service (preserves volumes)
	docker compose down

docker-ps:           ## Show compose service status
	docker compose ps

docker-logs:         ## Follow logs for the gendia compose service
	docker compose logs -f $(COMPOSE_SERVICE)

docker-exec:         ## Open an interactive shell inside the running gendia container
	@docker compose ps --status running --services 2>/dev/null | grep -qx "$(COMPOSE_SERVICE)" \
		|| (echo "$(COMPOSE_SERVICE) service is not running. Try 'make docker-up' first."; exit 1)
	docker compose exec $(COMPOSE_SERVICE) bash

docker-host-shell:   ## Open a host-side shell on the Docker daemon's host (no-op on Docker Desktop)
	@if command -v limactl >/dev/null 2>&1 && limactl list 2>/dev/null | grep -q running; then \
		echo "Using Lima shell..."; limactl shell default; \
	elif command -v multipass >/dev/null 2>&1 && multipass list 2>/dev/null | grep -q docker; then \
		echo "Using Multipass shell..."; multipass shell docker; \
	else \
		echo "No remote Docker host detected (Lima / Multipass). On Docker Desktop the daemon"; \
		echo "runs inside a Linux VM that isn't directly shell-accessible — use 'make docker-exec'"; \
		echo "to enter the running gendia container instead."; \
	fi

clean:               ## Remove build artifacts
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
