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

Both modes read the same profile, so one `smoke-optimiser smoke` run (or its `--profile-only`
step) is enough to use either.

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

### `smoke` mode

1. **Profile your suite**:
   Run the optimiser in your project root. It will automatically detect your source code and profile your tests and build
   an optimised smoke test suite.
   ```bash
   uv run smoke-optimiser smoke
   ```
   This both writes a profile (`.smoke_profiling_data.json`) and, from it, a smoke suite
   (`.smoke_suite.json`).

2. **Run the smoke suite**:
   Use the `--smoke` flag with pytest to run only the selected high-value tests.
   ```bash
   uv run pytest --smoke
   ```

### `downwind` mode (test only what a change can reach)

```bash
uv run smoke-optimiser downwind
```

One command does the whole job, and there's no separate profiling step to run first: if
`.smoke_profiling_data.json` is missing, stale, or from another build, `downwind` runs the full
suite itself, instrumented, to produce one before it selects anything. Every run after that reads
your current working-tree diff against that profile, writes the result to `.downwind.json`, and
runs pytest against it (equivalent to `pytest --downwind` once `.downwind.json` is current — plain
`pytest --downwind` on its own would just replay whatever selection is already on disk, stale or
not). It exits with pytest's own exit code, so it can gate a commit.

Run it from **the directory holding your project's `pyproject.toml`**, which need not be the
repository root. Both commands take their configuration from that directory, write
`.smoke_profiling_data.json` and `.downwind.json` beside it, and give pytest that directory as its
cwd — so a project living in `api/` of a larger repository gets its own `rootdir`, `testpaths` and
`pythonpath`. Every path the profile stores is relative to the **repository** root, which is what
lets git's output and the profile's maps agree wherever you invoked from. Change which directory
you run in and the profile records the fact, so the next run rebuilds rather than quietly matching
nothing.

If your project lives in a subdirectory, invoke it so that the cwd actually changes — `uv
--directory api run smoke-optimiser downwind`, not `uv --project api run ...`, which discovers the
project without leaving the repository root.

## Smoke suite

`smoke-optimiser smoke` profiles the suite, then greedily picks the smallest set of tests that
meets a coverage target or time budget. The result is a **static** subset: the same tests come
back whatever you changed, because the selection is a bet on coverage-per-second rather than a
read of your diff. Use it where a fixed, fast gate is the goal and a categorical guarantee is not
— a local pre-push smoke check, or the first, fast stage of CI.

### How it works

1. **Profiling**: It runs your suite with `pytest-cov` and a custom hook to map every single branch execution to specific tests.
2. **Analysis**: It calculates the "efficiency" of every test (New Branches Covered / Duration).
3. **Greedy Selection**: It iteratively picks the most efficient test until your coverage target or time cap is reached.
4. **Redundancy Reporting**: It identifies "Coverage-equivalent groups" — sets of tests that cover the exact same logic.

### Common usages

#### Custom efficiency targets
By default, the tool tries to get maximum coverage within a 15-second time cap. You can tighten these bounds:
```bash
# Aim for 80% coverage, but stop if it takes longer than 5 seconds
uv run smoke-optimiser smoke --target-cov=80 --time-cap=5
```

#### Stabilising timing data
Test execution times can vary. Use `--iterations` to run the suite multiple times and average the results for a more stable smoke suite:
```bash
uv run smoke-optimiser smoke --iterations=3
```

#### Mandatory inclusion/exclusion
Force certain tests (or markers) to be included or excluded from the smoke suite:
```bash
# Always include authentication tests, but exclude anything marked as 'slow'
uv run smoke-optimiser smoke --include="tests/test_auth.py" --exclude="@pytest.mark.slow"
```
*Multiple items can be separated by commas.*

### Command-line arguments

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

### What gets instrumented

If you do not pass `--src` or set `cov_source`, the coverage target is worked out in this order, and
whichever step answers **says so on stderr** — what a profile instruments decides what it can ever
know, so a value you did not choose is never applied silently:

1. `[tool.coverage.run] source` (or `source_pkgs`) in `pyproject.toml` — your own explicit answer.
2. A `src/` directory beside that `pyproject.toml`.
3. A package named after the project in `pyproject.toml`.

If none of them answers, the run **stops** rather than instrumenting the whole repository. That is
not a safe default: it puts every `.py` file in the tree into the profile's scope, including ones
coverage.py never walks — anything outside an importable package, such as a directory of hook
scripts — and a file the profile can never know expires it on every run. The error hands back the
exact command to set a source, and the exact command to instrument everything on purpose if that is
genuinely what you want. This applies to `smoke` and to any `downwind` run that has to fall back to
an instrumented full-suite run — see below.

## Downwind

`smoke-optimiser downwind` selects **dynamically**, from your working-tree diff rather than a fixed
list. It answers, for the files you changed, which tests can possibly be affected — and where it
cannot answer, it runs everything. That makes its promise categorical rather than statistical: it
never skips a test that could catch a regression in what you touched, which is what makes it safe
to use as a precommit or pre-push gate rather than only a fast-but-lossy smoke check.

### How it works

`downwind` reuses the same profile `smoke` records — it needs no separate profiling step of its
own. From that profile it already has two maps: which tests executed which files
(`files_covered`), and which modules import which other modules (the import graph). Given `git
diff`, it unions:

- every test that has ever executed a changed file, and
- every test whose module transitively imports a changed file — this is what makes selection
  correct for files that only ever run at import time, such as a constants module, a Pydantic
  model, or a package re-export, which no test "executes" directly but every importer depends on.

Two cases exit `0` without running pytest at all: a clean working tree, and a change the profile
says no test reaches — the latter with a warning naming the files, since it can also mean the
profile is missing a route to the suite rather than that the files are genuinely untested.
Otherwise it runs pytest itself and exits with pytest's own exit code, so a failing selected test
fails the commit and a collection error never reads as a successful selective run.

### Command-line arguments

| Argument | Description | Default |
| :--- | :--- | :--- |
| `--profile-path` | Path of the profile to select from. | `.smoke_profiling_data.json` |
| `--downwind-file-path` | Path for the generated downwind selection file. | `.downwind.json` |
| `--pytest-args` | Extra arguments forwarded to the downwind pytest run. Deliberately separate from the profiling run's, whose coverage flags would otherwise instrument every commit. | `""` |
| `--regenerate-on-fallback` / `--no-regenerate-on-fallback` | Run a full-suite fallback under instrumentation, rewriting the profile it fell back from. | `True` |
| `--src` | Source directory/package to instrument when a fallback regenerates the profile. | discovered |

### A fallback repairs the map it fell back from

When `downwind` cannot answer — a profile that is missing, from an older build, unreadable, or one
whose maps have gone stale — it runs the full suite **under profiling instrumentation** and rewrites
the profile. The run that pays for the fallback is the run that repairs the map, so the next commit
selects a subset again. Without it staleness ratchets: the map expires and every commit pays a full
suite until someone remembers to regenerate by hand.

Nothing detaches and nothing fires later. The only suite that runs is the one you were already
waiting on; instrumenting it measured about +11% (61s → 68s on one real project).

The rewrite is conditional, because a partial profile is worse than a stale one — an import graph
missing edges reads as *nothing imports those modules* rather than as *unknown*, so a change to them
would select no tests. A run that did not produce complete data (killed part-way, an xdist worker
that died) leaves the previous profile exactly where it was and says so, and the command exits
non-zero so a regeneration that quietly failed cannot pass for a green run. A fallback whose *tests*
fail is not that case: outcomes record which tests failed and the map is as good either way, so it
still rewrites the profile and still reports pytest's exit code.

The instrumented run uses the profiling `pytest_args`, not `--pytest-args` above. A per-commit `-x`
or `-k` would narrow collection into a profile of part of the suite — one that every completeness
check passes — and land it on top of a good one. The exception is the pytest-xdist distribution
flags (`-n`, `--numprocesses`, `--dist`, `--maxprocesses`), which carry over: they change only how
the same tests are spread across processes, so the profile is of the whole suite either way, and
dropping them would run a parallel suite serially. Durations recorded that way are contended, so
`smoke` will not rank such a profile without `--allow-parallel-durations` — downwind selection is
unaffected.

It instruments the coverage root the profile being replaced was recorded under, so a fallback
reproduces the profile it replaces rather than substituting a different one. An explicitly
configured source still wins over that.

An unreadable profile is moved to `<profile>.corrupt` before being rebuilt, so the evidence survives
for a bug report. The one fault a fallback cannot repair is a profile recording no scope roots: that
names a misconfiguration, so regenerating would produce another one just like it. That case stops
the run and names the settings to fix.

Pass `--no-regenerate-on-fallback` (or set `regenerate_on_fallback = false`) to run the fallback
plain and be told how to rebuild the profile yourself.

### Data files

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

## `pytest` plugin

`smoke-optimiser` registers a pytest plugin that filters collection down to whichever selection
file a mode wrote.

| Argument | Description | Default |
| :--- | :--- | :--- |
| `--smoke` | Activates the plugin; filters collection to the smoke suite. | `False` |
| `--smoke-file-path` | Path to the smoke suite JSON file to use. | `.smoke_suite.json` |
| `--downwind` | Activates the plugin; filters collection to the downwind selection. | `False` |
| `--downwind-file-path` | Path to the downwind selection JSON file to use. | `.downwind.json` |

`--smoke` and `--downwind` cannot be used together: running both would select only the tests in
both, which keeps neither promise.
</content>
