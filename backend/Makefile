# Backend Makefile: build Python code and run the unified API server.
# Run from backend/ (e.g. make install, make run).

PYTHON ?= python
VENV ?= .venv
ifeq ($(OS),Windows_NT)
  VENV_BIN = $(VENV)/Scripts
  VENV_PYTHON = $(VENV_BIN)/python.exe
  VENV_PIP = $(VENV_BIN)/pip.exe
else
  VENV_BIN = $(VENV)/bin
  VENV_PYTHON = $(VENV_BIN)/python
  VENV_PIP = $(VENV_BIN)/pip
endif

REQUIREMENTS = agents/requirements.txt
PORT ?= 8080
HOST ?= 0.0.0.0

.DEFAULT_GOAL := help
.PHONY: help install install-dev lint lint-fix test build run deploy clean

help:
	@echo "Backend targets:"
	@echo "  make install     - Create venv and install dependencies"
	@echo "  make install-dev - Install with dev deps (pytest, ruff)"
	@echo "  make lint       - Run ruff check and format check"
	@echo "  make lint-fix   - Run ruff with --fix and format"
	@echo "  make test       - Run pytest (agents + unified_api)"
	@echo "  make build      - install, lint, test"
	@echo "  make run        - Start unified API server (dev, foreground)"
	@echo "  make deploy     - Start API server (production: no reload)"
	@echo "  make clean      - Remove .venv and caches"

install: $(REQUIREMENTS)
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -r $(REQUIREMENTS)

install-dev: install
	$(VENV_PIP) install pytest ruff

lint:
	$(VENV_PYTHON) -m ruff check .
	$(VENV_PYTHON) -m ruff format --check .

lint-fix:
	$(VENV_PYTHON) -m ruff check . --fix
	$(VENV_PYTHON) -m ruff format .

test:
	$(VENV_PYTHON) -m pytest -v

build: install-dev lint test

run:
	$(VENV_PYTHON) run_unified_api.py --host $(HOST) --port $(PORT) --reload

deploy:
	$(VENV_PYTHON) run_unified_api.py --host $(HOST) --port $(PORT) --workers 4 --log-level warning

clean:
	rm -rf $(VENV) .pytest_cache
	-find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
