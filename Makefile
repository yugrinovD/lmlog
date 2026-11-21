COMPOSE ?= docker compose
MODEL ?= qwen2.5:3b

.PHONY: help build demo generate ingest scan errors explain trends bench \
        serve serve-down serve-test daemon-up daemon-down ai-up ai-down pull-model \
        llm llm-down test lint down clean

help:
	@echo "lmlog demo targets"
	@echo "  make demo        build, generate logs, ingest, scan and show errors"
	@echo "  make generate    generate synthetic logs (see LMLOG_DEMO_LINES)"
	@echo "  make ingest      read logs and save a snapshot"
	@echo "  make scan        show findings"
	@echo "  make errors      show error clusters"
	@echo "  make explain     explain findings (heuristic, no LLM needed)"
	@echo "  make trends      show anomalies from daily history"
	@echo "  make serve       start the optional HTTP API on :8090"
	@echo "  make daemon-up   start background incremental ingest"
	@echo "  make ai-up       start standalone-dev Ollama and pull the model"
	@echo "  make pull-model  pull the pinned model into the running Ollama"
	@echo "  make bench       measure ingest throughput"
	@echo "  make test        run the test suite"
	@echo "  make lint        run ruff"
	@echo "  make down        stop all services"
	@echo "  make clean       remove local runtime data"

build:
	$(COMPOSE) build

demo: build
	$(COMPOSE) run --rm generator
	$(COMPOSE) run --rm lmlog ingest
	$(COMPOSE) run --rm lmlog scan
	$(COMPOSE) run --rm lmlog errors

generate: build
	$(COMPOSE) run --rm generator

ingest: build
	$(COMPOSE) run --rm lmlog ingest

scan: build
	$(COMPOSE) run --rm lmlog scan

errors: build
	$(COMPOSE) run --rm lmlog errors

explain: build
	$(COMPOSE) run --rm lmlog explain --no-llm

trends: build
	$(COMPOSE) run --rm lmlog trends

bench: build
	$(COMPOSE) run --rm lmlog bench --lines 1000000

serve: build
	$(COMPOSE) --profile serve up -d --build serve

serve-down:
	$(COMPOSE) --profile serve stop serve

serve-test:
	$(COMPOSE) --profile serve run --rm serve serve --help >/dev/null
	@echo "serve image is ready"

daemon-up: build
	$(COMPOSE) --profile daemon up -d daemon

daemon-down:
	$(COMPOSE) --profile daemon stop daemon

# Standalone development only. Do not run together with the full infra stack:
# both would start separate Ollama containers and load the model twice.
ai-up:
	$(COMPOSE) --profile ai up -d ollama
	$(COMPOSE) --profile ai run --rm ollama-pull

ai-down:
	$(COMPOSE) --profile ai stop ollama

pull-model:
	$(COMPOSE) --profile ai run --rm ollama-pull

# Backward-compatible aliases.
llm: ai-up
llm-down: ai-down

test:
	python3 -m pytest

lint:
	ruff check .
	ruff format --check .

down:
	$(COMPOSE) --profile daemon --profile ai --profile llm --profile serve down --remove-orphans

clean:
	rm -rf data logs *.db *.db-wal *.db-shm .pytest_cache .ruff_cache .coverage htmlcov
