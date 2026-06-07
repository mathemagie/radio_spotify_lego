PYTHON := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: test test-one check install run

test: ## Run the full test suite
	$(PYTHON) -m unittest -v

test-one: ## Run one test class, e.g. make test-one CLASS=TestSearch
	$(PYTHON) -m unittest test_lego_radio.$(CLASS) -v

check: ## Syntax check
	$(PYTHON) -m py_compile lego_radio.py

install: ## Install dependencies
	$(PIP) install -r requirements.txt

run: ## Run the app
	$(PYTHON) lego_radio.py
