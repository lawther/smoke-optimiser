# BEGIN SHARED RECIPES (DEFAULT) sha256:18670817947290ab
# Generated from agent_rules/snippets/default.just. Do not edit inside this block:
#      edit snippets/default.just, then run `just sync-justfile-recipes` in agent_rules.

# List available recipes
default:
    @just --list

# END SHARED RECIPES (DEFAULT)

# The runner the shared ai_readiness recipes call; declared in agent_rules/repos.toml.
uv_run := "uv run"

# The directory the shared downwind recipe cds into; declared in agent_rules/repos.toml.
python_project_dir := "."

# Run all checks (lint, typecheck, test)
check: lint typecheck test

# Run linting and formatting
lint:
    uv run ruff format
    uv run ruff check --fix
    just extra-lints

# Check for bare dict/tuple return types and classes defined inside functions (ML001, ML002)
extra-lints:
    @uv run ml-lints smoke_optimiser/

# Format the code
format:
    uv run ruff format

# Run type checks
typecheck:
    uv run ty check --exclude scripts/analyse_ai_readiness.py
    just lint-pep723

# Run tests
test:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Running tests..."
    cov_json=$(mktemp)
    trap 'rm -f "$cov_json"' EXIT
    uv run pytest --cov=smoke_optimiser --cov-branch --cov-report=json:"$cov_json"
    uv run python scripts/branch_summary.py "$cov_json"
    echo "✅ Tests passed!"

# Run tests with coverage
test-cov:
    uv run pytest --cov=smoke_optimiser

# Run formatting, linting and tests (quiet on success, shows errors on failure)
# This Justfile is the Single Source Of Truth (SSOT) for all pre-commit checks.
precommit:
    #!/usr/bin/env bash
    if [[ "$(git config core.hooksPath 2>/dev/null)" != ".githooks" ]]; then
        echo "Git hooks not configured — installing now..."
        just setup-git-hooks
    fi
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
        just extra-lints
        xargs -r -0 git add < "$staged_list"
        uv run ty check --exclude scripts/analyse_ai_readiness.py
        just lint-pep723
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
    @git config core.hooksPath .githooks
    @chmod +x .githooks/pre-commit
    @echo "✅ Git hooks set up!"

# BEGIN SHARED RECIPES (AI_READINESS) sha256:925735e38c225a7b
# Generated from agent_rules/snippets/ai_readiness.just. Do not edit inside this block:
#      edit snippets/ai_readiness.just, then run `just sync-justfile-recipes` in agent_rules.

# Analyse project source files to evaluate AI agent readiness
analyse-ai-readiness *args:
    @{{uv_run}} scripts/analyse_ai_readiness.py {{args}}

# Check source file size thresholds for AI agent readiness (fails if threshold breached)
check-ai-readiness *args:
    @{{uv_run}} scripts/analyse_ai_readiness.py --fail-on {{args}}

# END SHARED RECIPES (AI_READINESS)

# BEGIN SHARED RECIPES (CHECK_HOOKS_DRIFT) sha256:b59ea25e66824683
# Generated from agent_rules/snippets/check_hooks_drift.just. Do not edit inside this block:
#      edit snippets/check_hooks_drift.just, then run `just sync-justfile-recipes` in agent_rules.

# Verify .claude/hooks/ still matches agent_rules/hooks/. Requires the
# agent_rules repo as a sibling checkout; local/pre-commit only for now.
# Wiring this into CI needs agent_rules reachable there — see agent_rules-403.
check-hooks-drift:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! -x ../agent_rules/sync_hooks.py ]]; then
        echo "error: ../agent_rules/sync_hooks.py not found — clone the agent_rules repo as a sibling" >&2
        exit 2
    fi
    ../agent_rules/sync_hooks.py check .

# END SHARED RECIPES (CHECK_HOOKS_DRIFT)

# BEGIN SHARED RECIPES (DOWNWIND) sha256:8af83ba867138284
# Generated from agent_rules/snippets/downwind.just. Do not edit inside this block:
#      edit snippets/downwind.just, then run `just sync-justfile-recipes` in agent_rules.

# Run every test downwind of the working-tree diff; full suite when it cannot tell.
downwind:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{python_project_dir}}"
    # CI never persists the profile (it is gitignored), so a regenerated one dies with
    # the runner -- every run would pay for instrumentation nothing ever reads. A missing
    # profile still runs the full suite, which is the backstop CI is for.
    #
    # Spelled as two calls rather than an array of flags: under `set -u`, bash 3.2 --
    # which is what macOS ships -- treats expanding an empty array as an unbound variable.
    if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
        uv run smoke-optimiser downwind --no-regenerate-on-fallback
    else
        uv run smoke-optimiser downwind
    fi

# END SHARED RECIPES (DOWNWIND)

# BEGIN SHARED RECIPES (PEP723) sha256:90afb09325f1476f
# Generated from agent_rules/snippets/pep723.just. Do not edit inside this block:
#      edit snippets/pep723.just, then run `just sync-justfile-recipes` in agent_rules.

# Type-check this repo's PEP 723 scripts in an ephemeral env built from each script's own
# inline metadata, since ty resolves imports against the project's .venv and has no notion
# of PEP 723 isolation -- checking the script via the project's own `ty check` fails or
# silently passes depending on whether the project's .venv happens to already carry the
# same packages. Drop this recipe, and this file's exclusion from the main `ty check`
# invocation, once astral-sh/ty#691 lands.
lint-pep723:
    @uvx --with-requirements scripts/analyse_ai_readiness.py ty check scripts/analyse_ai_readiness.py

# END SHARED RECIPES (PEP723)
