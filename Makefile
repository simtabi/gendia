.PHONY: help install install-dev test lint typecheck status sync inventory release audit cleanup verify mirror init conventions docker-build docker-shell docker-push clean

PYTHON ?= python3
UV     ?= uv
ARGS   ?=

help:                ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

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

typecheck:           ## Run mypy
	$(PYTHON) -m mypy src

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

# --- Docker -------------------------------------------------------------------

docker-build:        ## Build the gendia Docker image
	docker build -t gendia:latest .

docker-shell:        ## Drop into a gendia container with config + .env mounted
	docker run --rm -it \
		--env-file "$(HOME)/.config/gendia/.env" \
		-v "$(HOME)/.config/gendia:/config:ro" \
		-v "$(HOME)/.ssh:/home/gendia/.ssh:ro" \
		-v "$(PWD):/work" \
		--entrypoint bash gendia:latest

docker-run:          ## VERB=sync make docker-run — run any gendia verb in container
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

clean:               ## Remove build artifacts
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
