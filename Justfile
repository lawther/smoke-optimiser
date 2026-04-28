# List available recipes
default:
    @just --list

# Run all checks (lint, typecheck, test)
check: lint typecheck test

# Run linting and formatting
lint:
    uv run ruff format
    uv run ruff check --fix

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
# This Justfile is the Single Source Of Truth (SSOT) for all pre-commit checks.
precommit:
    #!/usr/bin/env bash
    echo "Running precommit checks..."
    uv lock --check || { echo "❌ uv.lock is out of sync with pyproject.toml"; exit 1; }
    tmpfile=$(mktemp)
    staged_list=$(mktemp)
    trap 'rm -f "$tmpfile" "$staged_list"' EXIT
    git diff --cached --name-only -z --diff-filter=d > "$staged_list"
    (
        set -e
        just _lint-justfile
        uv run ruff format
        uv run ruff check --fix
        xargs -r -0 git add < "$staged_list"
        uv run ty check
        uv run pytest
    ) > "$tmpfile" 2>&1
    status=$?
    if [ $status -ne 0 ]; then
        cat "$tmpfile"
        exit $status
    fi
    echo "✅ Precommit checks passed!"

# [private] Ensure Justfile recipes don't use && chains (which suppress set -e)
_lint-justfile:
    #!/usr/bin/env bash
    set -euo pipefail
    violations=$(awk '
        /^[[:space:]]+#!/ { in_shebang = 1 }
        /^[^[:space:]]/ && NF > 0 { in_shebang = 0 }
        !in_shebang && /&&/ && !/^[[:space:]]*#/ { print NR": "$0 }
    ' Justfile)
    if [[ -n "$violations" ]]; then
        echo "❌ Justfile recipes must not use && chains. Use separate lines for reliable error reporting."
        echo "$violations"
        exit 1
    fi

# Setup the development environment from a fresh clone
setup-dev:
    @uv sync
    @just setup-git-hooks
    @echo "✅ Development environment setup complete!"

# Setup local git hooks
setup-git-hooks:
    @echo "Setting up local git hooks..."
    @echo "#!/bin/sh" > .git/hooks/pre-commit
    @echo "# This hook invokes the Justfile, which is the Single Source Of Truth for precommit logic." >> .git/hooks/pre-commit
    @echo "# DO NOT add precommit logic here; add it to the 'precommit' recipe in the Justfile." >> .git/hooks/pre-commit
    @echo "just precommit" >> .git/hooks/pre-commit
    @chmod +x .git/hooks/pre-commit
    @echo "✅ Git hooks set up!"
