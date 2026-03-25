.PHONY: lint test fmt check run clean progress ralph principles quality-scan

lint:
	uvx ruff check engine/ tests/
	uvx ruff format --check engine/ tests/

fmt:
	uvx ruff check --fix engine/ tests/
	uvx ruff format engine/ tests/

test:
	python -m pytest tests/ -v

principles:
	python -m engine.golden_principles engine

check: lint test principles

run:
	python -m engine $(ARGS)

progress:
	python scripts/gen-progress.py

ralph:
	./scripts/run-ralph-loop.sh

quality-scan:
	python -m engine.quality_scanner engine

clean:
	rm -rf output/ .pytest_cache/ __pycache__/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
