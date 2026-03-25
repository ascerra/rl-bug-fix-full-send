.PHONY: lint test fmt check run clean progress

lint:
	uvx ruff check engine/ tests/
	uvx ruff format --check engine/ tests/

fmt:
	uvx ruff check --fix engine/ tests/
	uvx ruff format engine/ tests/

test:
	python -m pytest tests/ -v

check: lint test

run:
	python -m engine $(ARGS)

progress:
	python scripts/gen-progress.py

clean:
	rm -rf output/ .pytest_cache/ __pycache__/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
