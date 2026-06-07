PYTHON := .venv/bin/python
PIP := .venv/bin/pip
export PYTHONPATH := src

.PHONY: test test-one check install run

test: ## Run the full test suite
	$(PYTHON) -m unittest discover -s tests -v

test-one: ## Run one test class, e.g. make test-one CLASS=TestSearch
	$(PYTHON) -m unittest tests.test_lego_radio.$(CLASS) -v

check: ## Syntax check
	$(PYTHON) -m py_compile src/lego_radio.py

install: ## Install dependencies
	$(PIP) install -r requirements.txt

run: ## Run the app
	$(PYTHON) src/lego_radio.py
