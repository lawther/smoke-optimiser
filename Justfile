# BEGIN FLEET RECIPES (CORE)
# Generated from agent_rules payload/core/recipes.just. Do not edit inside this block:
#      edit it there, then run `just sync` in agent_rules.

# List available recipes
default:
    @just --list

# Fail if a file, block or hook registration the fleet manages here has changed since sync
# wrote it. Reads only this repo, so it runs the same in the git hook and in CI.
check-fleet:
    @uv run --script .fleet/core/check_integrity.py

# Every check the fleet requires of this repo. This repo's own precommit depends on it, so a
# check added here reaches every repo's gate with the next sync.
precommit-fleet: check-fleet

# Land this repo's open Dependabot PRs on main, one tested commit per PR. Never pushes.
apply-dependabot *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! -x ../agent_rules/apply_dependabot.py ]]; then
        echo "error: ../agent_rules/apply_dependabot.py not found -- clone the agent_rules repo as a sibling" >&2
        exit 2
    fi
    ../agent_rules/run_shared_script.py --check-failed-exit 3 -- ../agent_rules/apply_dependabot.py . {{args}}

# Apply this repo's Dependabot security fixes, then upgrade every other dependency locally,
# one tested commit each. Never pushes.
update-deps *args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! -x ../agent_rules/update_deps.py ]]; then
        echo "error: ../agent_rules/update_deps.py not found -- clone the agent_rules repo as a sibling" >&2
        exit 2
    fi
    ../agent_rules/run_shared_script.py --check-failed-exit 3 -- ../agent_rules/update_deps.py . {{args}}

# END FLEET RECIPES (CORE)

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
    just lint-workflows

# Check for bare dict/tuple return types and classes defined inside functions (ML001, ML002)
extra-lints:
    @uv run ml-lints smoke_optimiser/

# Format the code
format:
    uv run ruff format

# Run type checks
typecheck:
    uv run ty check
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
        uv run ty check
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

# BEGIN FLEET RECIPES (SCRIPTS)
# Generated from agent_rules payload/scripts/recipes.just. Do not edit inside this block:
#      edit it there, then run `just sync` in agent_rules.

# Analyse project source files to evaluate AI agent readiness
analyse-ai-readiness *args:
    @{{uv_run}} shared_scripts/analyse_ai_readiness.py {{args}}

# Check source file size thresholds for AI agent readiness (fails if threshold breached)
check-ai-readiness *args:
    @{{uv_run}} shared_scripts/analyse_ai_readiness.py --fail-on {{args}}

# Type-check the synced PEP 723 scripts in shared_scripts/ in an ephemeral env built from each
# script's own inline metadata. ty resolves imports against the project's .venv and has no
# notion of PEP 723 isolation, so checking them via the project's own `ty check` fails or
# silently passes depending on whether the .venv happens to carry the same packages. Keep
# shared_scripts/ out of the project's own `ty check` invocation, and call this recipe from
# the project's lint recipe. Drop both once astral-sh/ty#691 lands.
lint-pep723:
    @uvx --with-requirements shared_scripts/analyse_ai_readiness.py ty check shared_scripts/analyse_ai_readiness.py

# END FLEET RECIPES (SCRIPTS)

# BEGIN FLEET RECIPES (SMOKE)
# Generated from agent_rules payload/smoke/recipes.just. Do not edit inside this block:
#      edit it there, then run `just sync` in agent_rules.

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

# END FLEET RECIPES (SMOKE)

# BEGIN FLEET RECIPES (GIT_HOOKS)
# Generated from agent_rules payload/git_hooks/recipes.just. Do not edit inside this block:
#      edit it there, then run `just sync` in agent_rules.

# Sets this repo's own local git config to prefer linear history, overriding whatever
# a clone's global config says. `pull.rebase` and `merge.ff` live in .git/config, which
# git deliberately never lets a repo commit -- so this is the one-off `setup-git-hooks`
# already asks a fresh clone to run, not something a sync can write for you. Actual
# enforcement (for a clone that skips this, or whose global config wins some other way)
# is the `pre-merge-commit` and `pre-push` hooks in .githooks/, which reject merge
# commits regardless of what these two settings say.
setup-linear-history:
    @git config pull.rebase true
    @git config merge.ff only
    @echo "Linear history preferences set for this clone"

# END FLEET RECIPES (GIT_HOOKS)

# BEGIN FLEET RECIPES (GITHUB)
# Generated from agent_rules payload/github/recipes.just. Do not edit inside this block:
#      edit it there, then run `just sync` in agent_rules.

# zizmor's version is agent_rules' locked one, stamped in by the sync, so update-deps there
# upgrades it fleet-wide. --offline keeps the audit reproducible and token-free in the git hook;
# known-vulnerable actions reach the fleet through Dependabot security updates instead. zizmor
# exits 3 when it collects no input, which is a repo with no workflows, actions or dependabot.yml.
#
# Audit this repo's GitHub Actions workflows, composite actions and dependabot.yml with zizmor.
lint-workflows:
    #!/usr/bin/env bash
    set -uo pipefail
    status=0
    uvx zizmor@1.32.0 --offline . || status=$?
    if [ "$status" -eq 3 ]; then
        echo "lint-workflows: nothing to audit"
        exit 0
    fi
    exit "$status"

# END FLEET RECIPES (GITHUB)
