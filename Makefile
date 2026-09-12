.PHONY: install lint test serve mock check

install:
	uv sync --extra dev

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src

test:
	uv run pytest -q

serve:
	uv run poke-hermes-bridge serve

mock:
	uv run poke-hermes-bridge mock-hermes

check:
	uv run poke-hermes-bridge check
