# BEGIN FLEET RECIPES (CORE)
# Generated from fleet payload/core/recipes.just. Do not edit inside this block:
#      edit it there, then run `just sync` in fleet.

# List available recipes
default:
    @just --list

# Fail if a file, block or hook registration the fleet manages here has changed since sync
# wrote it. Reads only this repo, so it runs the same in the git hook and in CI.
check-fleet:
    @uv run --script .fleet/core/check_integrity.py

# sync writes the gate list into the next line, so it differs between repos. This repo's own
# precommit depends on it, so a check added to a family reaches the gate of every repo that
# carries the family with the next sync.
#
# Run every check the fleet requires of this repo: the integrity check and each family's gate.
precommit-fleet: check-fleet fleet-gate-core fleet-gate-rules fleet-gate-lint fleet-gate-github

# Fail if the repo's precommit skips precommit-fleet, its CI lacks uv, or its fleet.toml is invalid.
fleet-gate-core:
    @uv run --script .fleet/core/check_recipes_wired.py
    @uv run --script .fleet/core/fleet_config.py

# END FLEET RECIPES (CORE)

# The runner the shared ai_readiness recipes call; declared in fleet/repos.toml.
uv_run := "uv run"

# The directory the shared downwind recipe cds into; this repo's fleet.toml sets it, and its default is the repo root.
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
    uv run ty check

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
precommit: precommit-fleet
    #!/usr/bin/env bash
    if [[ "$(git config core.hooksPath 2>/dev/null)" != ".fleet/git_hooks" ]]; then
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

# BEGIN FLEET RECIPES (SCRIPTS)
# Generated from fleet payload/scripts/recipes.just. Do not edit inside this block:
#      edit it there, then run `just sync` in fleet.

# Analyse project source files to evaluate AI agent readiness
analyse-ai-readiness *args:
    @{{uv_run}} .fleet/scripts/analyse_ai_readiness.py {{args}}

# Check source file size thresholds for AI agent readiness (fails if threshold breached)
check-ai-readiness *args:
    @{{uv_run}} .fleet/scripts/analyse_ai_readiness.py --fail-on {{args}}

# END FLEET RECIPES (SCRIPTS)

# BEGIN FLEET RECIPES (SMOKE)
# Generated from fleet payload/smoke/recipes.just. Do not edit inside this block:
#      edit it there, then run `just sync` in fleet.

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
# Generated from fleet payload/git_hooks/recipes.just. Do not edit inside this block:
#      edit it there, then run `just sync` in fleet.

# Sets this repo's own local git config to prefer linear history, overriding whatever
# a clone's global config says. `pull.rebase` and `merge.ff` live in .git/config, which
# git deliberately never lets a repo commit -- so this is the one-off `setup-git-hooks`
# already asks a fresh clone to run, not something a sync can write for you. Actual
# enforcement (for a clone that skips this, or whose global config wins some other way)
# is the `pre-merge-commit` and `pre-push` hooks in .fleet/git_hooks/, which reject merge
# commits regardless of what these two settings say.
setup-linear-history:
    @git config pull.rebase true
    @git config merge.ff only
    @echo "Linear history preferences set for this clone"

# Refuses rather than clobbers. git reads exactly one hooks directory, so silently
# overwriting core.hooksPath disables whatever was there -- which is precisely how four
# repos lost their beads integration without a word (ar-x4l). `bd init` does exactly
# this, so the refusal is needed; `check-git-hooks-active` is what reports it if bd wins the
# race anyway. An unset value and the legacy `.githooks` are both safe to replace, and a value is
# judged by the directory it names, so an absolute path to either counts as that directory.
#
# Point git at .fleet/git_hooks/, so `just precommit` runs before each commit here.
setup-git-hooks:
    #!/usr/bin/env bash
    set -euo pipefail
    current=$(git config core.hooksPath || true)
    root=$(git rev-parse --show-toplevel)
    case "$current" in
        /*) named="$current" ;;
        *) named="$root/$current" ;;
    esac
    named="${named%/}"
    if [ -n "$current" ] && [ "$named" != "$root/.githooks" ] && [ "$named" != "$root/.fleet/git_hooks" ]; then
        echo "core.hooksPath is already $current -- refusing to overwrite it." >&2
        echo "git reads one hooks directory only; merge the two before setting this." >&2
        exit 1
    fi
    git config core.hooksPath .fleet/git_hooks
    echo "Git hooks set up"

# END FLEET RECIPES (GIT_HOOKS)

# BEGIN FLEET RECIPES (GITHUB)
# Generated from fleet payload/github/recipes.just. Do not edit inside this block:
#      edit it there, then run `just sync` in fleet.

# zizmor's version is fleet's locked one, stamped in by the sync, so update-deps there
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

# Exemptions from these checks are the repo's own, in its fleet.toml.
#
# Fail if a workflow fails zizmor, an action is not pinned to a commit SHA, or dependabot.yml can open version updates.
fleet-gate-github: lint-workflows
    @uv run --script .fleet/github/check_actions_pinned.py
    @uv run --script .fleet/github/check_dependabot_security.py

# END FLEET RECIPES (GITHUB)

# BEGIN FLEET RECIPES (RULES)
# Generated from fleet payload/rules/recipes.just. Do not edit inside this block:
#      edit it there, then run `just sync` in fleet.

# Fail if a generated Beads integration block has reappeared in the repo's root markdown.
fleet-gate-rules:
    @uv run --script .fleet/rules/check_beads_block.py

# END FLEET RECIPES (RULES)

# BEGIN FLEET RECIPES (LINT)
# Generated from fleet payload/lint/recipes.just. Do not edit inside this block:
#      edit it there, then run `just sync` in fleet.

# Fail if the repo's ruff config does not extend the shared one, pyproject.toml carries a table the delivered config overrides, or anything names a Python version other than .python-version.
fleet-gate-lint:
    @uv run --script .fleet/lint/check_lint_config.py
    @uv run --script .fleet/lint/check_python_version.py

# END FLEET RECIPES (LINT)
