# Implementation Plan: smoke-optimiser pytest plugin (`--smoke`)

## Context

The pytest plugin is the consumer side of smoke-optimiser. After the CLI generates `.smoke_suite.json`, developers and CI run `pytest --smoke` to execute only the selected smoke tests. The plugin reads the smoke suite file, validates it, and deselects all tests not in the suite during pytest collection.

This plan assumes CLI commits 1 (project setup) and 9 (report generation, including `SmokeSuiteFile` Pydantic model and `read_smoke_suite()`) have already been implemented. The plugin reuses the shared `SmokeSuiteFile` model from `smoke_optimiser/reports/smoke_suite.py`.

## Dependencies

- `smoke_optimiser/reports/smoke_suite.py` — provides `SmokeSuiteFile` Pydantic model and `read_smoke_suite(path) -> SmokeSuiteFile`
- `pyproject.toml` — pytest11 entry point already configured: `smoke_optimiser = "smoke_optimiser.plugin"`

---

## Commit Sequence

### Commit 1: `feat: add pytest plugin option registration and smoke suite loading`

Register the plugin's CLI options with pytest and implement smoke suite file loading with full error handling per PRD §8.2.

**Files:**
- `smoke_optimiser/plugin.py`
  - `def pytest_addoption(parser: pytest.Parser) -> None`
    - Adds `--smoke` as a boolean flag (`action="store_true"`, default `False`)
    - Adds `--smoke-file-path` as a string option (default `.smoke_suite.json`)
  - `SUPPORTED_VERSIONS: frozenset[int] = frozenset({1})` — set of supported schema versions
  - `def _load_smoke_suite(config: pytest.Config) -> SmokeSuiteFile | None`
    - Returns `None` if `--smoke` not active
    - Reads file path from `--smoke-file-path` option
    - Calls `read_smoke_suite()` from `smoke_optimiser.reports.smoke_suite`
    - Error handling (each logs via `pytest.exit()` with returncode 1):
      - File not found → `"smoke-optimiser: smoke suite file not found: {path}"`
      - Malformed JSON / Pydantic validation error → `"smoke-optimiser: invalid smoke suite file: {path}: {error}"`
      - Unsupported `version` field → `"smoke-optimiser: unsupported smoke suite version {v} (supported: {supported})"`
- `tests/test_plugin.py`
  - Use `pytester` fixture (pytest's built-in test infrastructure for plugin testing)
  - Test: `--smoke` and `--smoke-file-path` appear in `pytest --help` output
  - Test: without `--smoke`, `_load_smoke_suite` returns `None`
  - Test: with `--smoke` and valid file, returns `SmokeSuiteFile`
  - Test: file not found → exits with code 1, error message mentions path
  - Test: malformed JSON → exits with code 1
  - Test: unsupported version (e.g. `"version": 999`) → exits with code 1
  - Test: missing required fields → exits with code 1

**Design notes:**
- Use `pytest.exit(msg, returncode=1)` for fatal errors rather than raising exceptions. This gives clean output without tracebacks.
- The `_load_smoke_suite` helper is a private function but tested indirectly through `pytester` integration tests.

---

### Commit 2: `feat: add test collection filtering for --smoke`

Implement the core deselection logic during pytest collection.

**Files to modify:**
- `smoke_optimiser/plugin.py`
  - `def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None`
    - If `--smoke` not active, return immediately (no-op)
    - Call `_load_smoke_suite(config)` to get the smoke suite
    - Build a `set[str]` of `test_id` values from `smoke_suite.smoke_tests`
    - Iterate over collected `items`:
      - Match each `item.nodeid` against the smoke test IDs set
      - If matched: keep (add to `selected` list)
      - If not matched: deselect (add to `deselected` list)
    - For each `test_id` in the smoke suite that was **not** found in the collection: log a warning via `config.issue_config_time_warning(...)` or `warnings.warn(...)`: `"smoke-optimiser: smoke test not found in collection: {test_id}"`
    - Call `config.hook.pytest_deselected(items=deselected)` to properly notify pytest of deselected tests
    - Replace `items[:]` with `selected` (standard pytest pattern for modifying collection in-place)
- `tests/test_plugin.py` (additional tests)
  - Test: with valid smoke suite containing 2 of 5 tests → only 2 run, 3 deselected
  - Test: smoke suite test not in collection → warning logged, remaining tests still run
  - Test: all smoke tests missing from collection → warning per test, 0 tests run
  - Test: empty `smoke_tests` list → 0 tests run, no errors
  - Test: without `--smoke` → all tests run normally (plugin is no-op)
  - Test: `--smoke-file-path` custom path works
  - Test: compatible with test parametrisation (parametrised node IDs match)

**Design notes:**
- Node ID matching must be exact string match against `item.nodeid`. Parametrised tests have IDs like `tests/test_foo.py::test_bar[param1]` — these must match exactly.
- The `items[:] = selected` pattern is the standard pytest way to modify collection in-place.
- Deselected items are reported via `pytest_deselected` hook so pytest's summary shows "X deselected".

---

### Commit 3: `feat: add plugin summary header showing smoke suite metadata`

Add a terminal header line when `--smoke` is active, showing which smoke suite file is being used and key stats.

**Files to modify:**
- `smoke_optimiser/plugin.py`
  - `def pytest_report_header(config: pytest.Config) -> str | None`
    - If `--smoke` not active, return `None`
    - Return a line like: `"smoke-optimiser: running smoke suite from .smoke_suite.json (42 tests, 80.0% coverage, profiled on ci-runner-04)"`
    - Reads summary stats from the loaded `SmokeSuiteFile`
  - Cache the loaded `SmokeSuiteFile` to avoid double-reading (store on `config` object via `config.stash` with a `StashKey`)
- Refactor: move file loading to `pytest_configure` hook so it happens once, store result in `config.stash`
  - `_smoke_suite_key = pytest.StashKey[SmokeSuiteFile]()`
  - `def pytest_configure(config: pytest.Config) -> None` — loads and stashes the smoke suite (or exits on error)
  - Update `pytest_collection_modifyitems` to read from stash
  - Update `pytest_report_header` to read from stash
- `tests/test_plugin.py` (additional tests)
  - Test: header line appears in output when `--smoke` active
  - Test: header contains file path and test count
  - Test: no header without `--smoke`
  - Test: file is loaded only once (not re-read for header and collection)

**Design notes:**
- `pytest.StashKey` is the modern (pytest 7.0+) way to store plugin state on config, replacing the older `config._metadata` pattern.
- Loading in `pytest_configure` ensures errors surface early, before collection begins.

---

## File Summary

| File | Created/Modified | Commits |
|------|-----------------|---------|
| `smoke_optimiser/plugin.py` | Created in 1, modified in 2–3 | All |
| `tests/test_plugin.py` | Created in 1, extended in 2–3 | All |

## Key Shared Dependencies

| Module | Import | Used For |
|--------|--------|----------|
| `smoke_optimiser.reports.smoke_suite` | `read_smoke_suite`, `SmokeSuiteFile` | Reading and validating `.smoke_suite.json` |

## Testing Strategy

All tests use the `pytester` fixture, which:
- Creates a temporary directory with test files
- Runs pytest in a subprocess (isolated from the test process)
- Captures stdout/stderr and exit codes
- Is the standard pytest-recommended way to test plugins

Each test creates:
1. A minimal `conftest.py` (if needed)
2. A test file with simple test functions
3. A `.smoke_suite.json` fixture file (valid or intentionally malformed)
4. Runs `pytester.runpytest("--smoke", ...)` and asserts on outcomes

## Verification

After all commits:
1. `uv run ruff check` — no lint errors
2. `uv run ruff format --check` — no formatting issues
3. `uv run pytest tests/test_plugin.py -v` — all plugin tests pass
4. Manual verification with a real project:
   - Generate `.smoke_suite.json` with the CLI (or create a fixture manually)
   - Run `pytest --smoke` — only smoke tests run
   - Run `pytest --smoke --smoke-file-path=custom.json` — custom path works
   - Run `pytest` (without `--smoke`) — all tests run normally
   - Delete the smoke suite file, run `pytest --smoke` — exits with error
