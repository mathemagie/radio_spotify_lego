PYTHON := .venv/bin/python
PIP := .venv/bin/pip
export PYTHONPATH := src

.PHONY: test test-one check install install-dev install-hooks lint format run

test: ## Run the full test suite
	$(PYTHON) -m unittest discover -s tests -v

test-one: ## Run one test class, e.g. make test-one CLASS=TestSearch
	$(PYTHON) -m unittest tests.test_lego_radio.$(CLASS) -v

check: ## Syntax check
	$(PYTHON) -m py_compile src/lego_radio.py

install: ## Install dependencies
	$(PIP) install -r requirements.txt

install-dev: install ## Install runtime and development dependencies
	$(PIP) install -r requirements-dev.txt

install-hooks: ## Install git hooks
	cp scripts/pre-commit .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit

lint: ## Run Ruff and Black checks
	.venv/bin/ruff check .
	.venv/bin/black --check .

format: ## Format code with Ruff and Black
	.venv/bin/ruff check --fix .
	.venv/bin/black .

run: ## Run the app
	$(PYTHON) src/lego_radio.py
