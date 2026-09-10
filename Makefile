.PHONY: setup test lint clean

PYTHON := python3
VENV := .venv

$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e ".[all,dev]"

setup: $(VENV)/bin/activate

test: setup
	$(VENV)/bin/pytest

lint: setup
	$(VENV)/bin/flake8 src/ tests/

clean:
	rm -rf $(VENV) build dist *.egg-info .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
