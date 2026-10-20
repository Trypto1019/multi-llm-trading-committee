PYTHON ?= python3

.PHONY: demo demo-debate test scan clean

demo:          ## one full committee cycle, offline (no keys, no network)
	$(PYTHON) demo.py --mock

demo-debate:   ## committee cycle + debate ranker, offline
	$(PYTHON) demo.py --mock --debate

test:
	$(PYTHON) -m pytest -q

scan:          ## requires gitleaks on PATH
	gitleaks dir --redact --no-banner .

clean:
	rm -rf out .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
