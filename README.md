# smoke-optimiser

`smoke-optimiser` is a tool and pytest plugin that profiles your test suite once and then selects
from it in two different ways.

**`smoke-optimiser smoke`** produces a minimal **smoke suite** — a subset of tests that delivers
maximum code coverage in minimum wall-clock time. It helps you find the "bang for buck" sweet
spot: for example, achieving 80% of your total branch coverage in only 5% of the total runtime.
It is a coverage bet: a fixed subset that makes no promise about what it skips.

**`smoke-optimiser downwind`** selects on what you actually changed: given your working-tree diff,
it runs every test the profile says those changes can reach — every test that has ever executed a
file you touched, plus every test whose module transitively imports one. Where it cannot answer
from the profile it says so and runs the whole suite. That promise is categorical rather than
statistical, which is what makes it usable as a precommit gate.

## Installation

Add `smoke-optimiser` as a development dependency in your project:

```bash
uv add --dev smoke-optimiser
```

Or with pip:

```bash
pip install smoke-optimiser
```

This will make the `smoke-optimiser` command available in your environment and register the pytest plugin automatically.

## Quickstart

1. **Generate the smoke suite**:
   Run the optimiser in your project root. It will automatically detect your source code and profile your tests.
   ```bash
   uv run smoke-optimiser smoke
   ```

2. **Run the smoke suite**:
   Use the `--smoke` flag with pytest to run only the selected high-value tests.
   ```bash
   uv run pytest --smoke
   ```

3. **Or run what your changes can reach**:
   `downwind` selects against your diff, using the profile that step 1 recorded. It runs pytest
   itself, and exits with pytest's own exit code, so it can gate a commit.
   ```bash
   uv run smoke-optimiser downwind
   ```
   It must be run from the repository root: git reports repo-relative paths and the profile's
   paths are relative to where it was profiled from, so anywhere else the two stop agreeing.

## Common Usages

### Custom Efficiency Targets
By default, the tool tries to get maximum coverage within a 15-second time cap. You can tighten these bounds:
```bash
# Aim for 80% coverage, but stop if it takes longer than 5 seconds
uv run smoke-optimiser smoke --target-cov=80 --time-cap=5
```

### Stabilising Timing Data
Test execution times can vary. Use `--iterations` to run the suite multiple times and average the results for a more stable smoke suite:
```bash
uv run smoke-optimiser smoke --iterations=3
```

### Mandatory Inclusion/Exclusion
Force certain tests (or markers) to be included or excluded from the smoke suite:
```bash
# Always include authentication tests, but exclude anything marked as 'slow'
uv run smoke-optimiser smoke --include="tests/test_auth.py" --exclude="@pytest.mark.slow"
```
*Multiple items can be separated by commas.*

## Command-line Arguments

### `smoke-optimiser smoke` (Generator)

| Argument | Description | Default |
| :--- | :--- | :--- |
| `--src` | The source directory or package to measure coverage for. | *Discovered* |
| `--iterations` | Number of times to run profiling to average timing data. | `1` |
| `--time-cap` | Maximum wall-clock runtime (seconds) of the smoke suite. | `15.0` |
| `--target-cov` | Target % of the full suite's branch coverage to achieve. | `100.0` |
| `--include` | Comma-separated list of tests, files, or markers to force include. | `[]` |
| `--exclude` | Comma-separated list of tests, files, or markers to force exclude. | `[]` |
| `--pytest-args` | Extra arguments forwarded to pytest during profiling. | `""` |
| `--output-json` | Path for the generated smoke suite definition file. | `.smoke_suite.json` |
| `--profile-only` | Record the profile and stop, skipping the optimisation phase. | `False` |
| `--optimise-only` | Run only the optimisation phase using existing profile data. | `False` |
| `--allow-ordered` / `--no-allow-ordered` | Suppress warning when `pytest-randomly` is not installed. | `False` |
| `--allow-parallel-durations` / `--no-allow-parallel-durations` | Rank a profile whose durations were recorded under `pytest-xdist` contention. | `False` |
| `--profile-path` | Path for the recorded profile, which `downwind` also reads. | `.smoke_profiling_data.json` |

### `smoke-optimiser downwind` (Change-based selection)

| Argument | Description | Default |
| :--- | :--- | :--- |
| `--profile-path` | Path of the profile to select from. | `.smoke_profiling_data.json` |
| `--downwind-file-path` | Path for the generated downwind selection file. | `.downwind.json` |
| `--pytest-args` | Extra arguments forwarded to the downwind pytest run. Deliberately separate from the profiling run's, whose coverage flags would otherwise instrument every commit. | `""` |

Its exit code is pytest's own, so a failing selected test fails the commit and a collection error
never reads as a successful selective run. Two cases exit 0 without running pytest at all: a clean
tree, and a change the profile says no test reaches — the latter with a warning naming the files,
since it can also mean the profile is missing a route to the suite.

With no profile at all it runs the full suite and prints the command to record one. A profile that
exists but cannot be read is an error, not a silent full-suite run.

#### Data files

Profiling records which files each test opens and which directories it globs, so a changed data
file is answered rather than assumed about. A changed fixture selects the tests that read it; a
changed README that nothing read selects nothing; a *new* file selects whatever the profile
measured about the directory holding it — the tests that glob it, and the tests that read its
neighbours — so adding a doc under `docs/` costs nothing while adding a fixture beside ones tests
use selects those tests.

Two things still force the full suite: a file read while the suite was starting up rather than by
any test, since nothing can be named as depending on it; and a file that defines the environment
every test runs in. Nothing opens a lockfile while the suite runs, so measurement would wrongly
report it as depended on by nothing:

```toml
[tool.smoke_optimiser]
extra_environment_files = ["deploy/*.tf", "ansible/*.yml"]
```

The shipped defaults already cover `uv.lock`, `poetry.lock`, `Pipfile.lock`, `requirements*.txt`,
`pyproject.toml`, `setup.py`, `setup.cfg`, `tox.ini`, `pytest.ini`, `.python-version`, `Dockerfile*`
and `docker-compose*`. The key is *extra* rather than a replacement so that naming your own cannot
silently drop the guard on a lockfile.

Patterns are matched against the repository-relative path and against the bare filename, so
`uv.lock` catches one at any depth while `deploy/*.tf` catches only those. An entry can only ever
add a full-suite run, which is why this is safe to configure where a list of paths to *ignore*
would not be: a wrong entry costs time rather than correctness.

### `pytest` (Plugin)

| Argument | Description | Default |
| :--- | :--- | :--- |
| `--smoke` | Activates the plugin; filters collection to the smoke suite. | `False` |
| `--smoke-file-path` | Path to the smoke suite JSON file to use. | `.smoke_suite.json` |
| `--downwind` | Activates the plugin; filters collection to the downwind selection. | `False` |
| `--downwind-file-path` | Path to the downwind selection JSON file to use. | `.downwind.json` |

`--smoke` and `--downwind` cannot be used together: running both would select only the tests in
both, which keeps neither promise.

## How it works

1. **Profiling**: It runs your suite with `pytest-cov` and a custom hook to map every single branch execution to specific tests.
2. **Analysis**: It calculates the "efficiency" of every test (New Branches Covered / Duration).
3. **Greedy Selection**: It iteratively picks the most efficient test until your coverage target or time cap is reached.
4. **Redundancy Reporting**: It identifies "Coverage-equivalent groups" — sets of tests that cover the exact same logic.
