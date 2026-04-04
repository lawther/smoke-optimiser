# List available recipes
default:
    @just --list

# Run all checks (lint, typecheck, test)
check: lint typecheck test

# Run linting and formatting
lint:
    uv run ruff check --fix
    uv run ruff format

# Format the code
format:
    uv run ruff format

# Run type checks
typecheck:
    uv run ty check

# Run tests
test:
    uv run pytest

# Run tests with coverage
test-cov:
    uv run pytest --cov=smoke_optimiser

# Run formatting, linting and tests (quiet on success, shows errors on failure)
precommit:
    @echo "Running precommit checks..."
    @tmpfile=$(mktemp); \
    trap 'rm -f "$$tmpfile"' EXIT; \
    if ! ( \
        uv run ruff format && \
        uv run ruff check --fix && \
        uv run ty check && \
        uv run pytest \
    ) > "$$tmpfile" 2>&1; then \
        cat "$$tmpfile"; \
        exit 1; \
    fi
    @echo "✅ Precommit checks passed!"

# Setup local git hooks
setup-git-hooks:
    @echo "Setting up local git hooks..."
    @echo "#!/bin/sh" > .git/hooks/pre-commit
    @echo "# This hook invokes the Justfile, which is the Single Source Of Truth for precommit logic." >> .git/hooks/pre-commit
    @echo "# DO NOT add precommit logic here; add it to the 'precommit' recipe in the Justfile." >> .git/hooks/pre-commit
    @echo "just precommit" >> .git/hooks/pre-commit
    @chmod +x .git/hooks/pre-commit
    @echo "✅ Git hooks set up!"
