# Product Requirements Document: smoke-optimiser

**Version:** 1.0
**Status:** Draft
**Date:** 2026-03-02

---

## 1. Overview

`smoke-optimiser` is a Python CLI tool and pytest plugin that analyses a project's test suite to produce a minimal, high-value **smoke suite** — a subset of tests that delivers maximum code coverage in minimum wall-clock time. It targets the "bang for buck" sweet spot: e.g. 80% of total branch coverage in 5% of total runtime.

### 1.1 Problem Statement

Running a full test suite before every commit or on every PR is slow. Developers skip local test runs, and CI/CD feedback loops stretch to minutes or hours. Projects need a principled way to identify which tests give the most coverage per second of runtime, so a fast smoke check can catch the majority of regressions.

### 1.2 Target Audience

| Audience | Use Case |
|---|---|
| Individual developers | Run a fast smoke test locally before committing (`pytest --smoke`) |
| CI/CD pipelines | Run a smoke gate on PRs for rapid feedback before the full suite |
| Tech leads / QA engineers | Understand test redundancy and coverage overlap across the suite |

### 1.3 Non-Goals (v1)

- Generating new tests to fill coverage holes (see §11 Future Work).
- Supporting test runners other than pytest (unittest, nose2 are future).
- Maintaining persistent state or automatic re-optimisation triggers.
- Enforcing test-ordering independence (we recommend `pytest-randomly` but do not fix ordering bugs).

---

## 2. Architecture

### 2.1 High-Level Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                      smoke-optimiser CLI                        │
│                                                                 │
│  ┌──────────────┐    ┌──────────────────┐    ┌───────────────┐  │
│  │   Profiler    │───▶│  Coverage Store   │───▶│  Optimiser    │  │
│  │  (Phase 1)    │    │  (intermediate)   │    │  (Phase 2)    │  │
│  └──────┬───────┘    └──────────────────┘    └──────┬────────┘  │
│         │                                           │           │
│         ▼                                           ▼           │
│  pytest + coverage.py                      .smoke_suite.json    │
│  --cov-context=test                        + human reports      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

                              ▼

┌─────────────────────────────────────────────────────────────────┐
│                     pytest plugin (--smoke)                      │
│                                                                 │
│  Reads .smoke_suite.json → filters collection → runs smoke suite│
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Component Breakdown

```
smoke_optimiser/
├── __main__.py          # CLI entry point
├── cli.py               # Argument parsing, config merging
├── config.py            # Config model (pyproject.toml + CLI)
├── profiler/
│   ├── runner.py        # Orchestrates pytest + coverage run
│   ├── parser.py        # Parses coverage JSON (ijson for large files)
│   └── models.py        # Per-test coverage data structures
├── optimiser/
│   ├── greedy.py        # Greedy set-cover algorithm
│   ├── filters.py       # Mandatory include/exclude, failing test exclusion
│   └── models.py        # SmokeResult, CoverageEquivalent, etc.
├── reports/
│   ├── smoke_suite.py   # .smoke_suite.json writer
│   ├── summary.py       # Human-readable reports (stdout/file)
│   └── repro.py         # Full canonical command reconstruction
├── plugin.py            # pytest plugin (--smoke flag)
└── py.typed
```

---

## 3. Delivery Mechanism

### 3.1 Package

- Distributed as a pip-installable Python package: `pip install smoke-optimiser`.
- Provides a CLI entry point: `smoke-optimiser` (or `python -m smoke_optimiser`).
- Provides a pytest plugin registered via `entry_points` (`pytest11`), activated by `--smoke`.

### 3.2 Operational Modes

The CLI supports three execution modes:

| Mode | Flag | Behaviour |
|---|---|---|
| Full (default) | _(none)_ | Run profiling, then optimisation. One-shot. |
| Profile only | `--profile-only` | Run the test suite under coverage instrumentation; produce intermediate data. |
| Optimise only | `--optimise-only` | Read existing intermediate data; run the greedy algorithm and emit outputs. Fails if intermediate data is absent. |

---

## 4. Configuration

### 4.1 Hierarchy

Configuration is resolved in this order (later wins):

1. Built-in defaults (hardcoded).
2. `pyproject.toml` under `[tool.smoke_optimiser]`.
3. CLI arguments.

### 4.2 Config Options

| Key (pyproject.toml) | CLI Flag | Type | Default | Description |
|---|---|---|---|---|
| `time_cap` | `--time-cap` | `float` (seconds) | `15.0` | Maximum wall-clock runtime of the smoke suite. Hard cap — never exceeded even if `target_cov` is not yet met. |
| `target_cov` | `--target-cov` | `float` (percent) | `100.0` | Target percentage of the full suite's branch coverage to achieve. `100` effectively degenerates to time-cap-only mode. |
| `include_mandatory` | `--include` | `list[str]` | `[]` | Tests or pytest markers (e.g. `@pytest.mark.smoke`) that **must** be in the smoke suite. |
| `exclude_mandatory` | `--exclude` | `list[str]` | `[]` | Tests or pytest markers (e.g. `@pytest.mark.slow`) that **must not** be in the smoke suite. |
| `pytest_args` | `--pytest-args` | `str` | `""` | Extra arguments forwarded to pytest during profiling. |
| `output_json` | `--output-json` | `path` | `./.smoke_suite.json` | Path for the smoke suite definition file. |
| `allow_ordered` | `--allow-ordered` | `bool` | `false` | Suppress the warning/error when `pytest-randomly` is not installed. |
| — | `--profile-only` | `flag` | `false` | Run only the profiling phase. |
| — | `--optimise-only` | `flag` | `false` | Run only the optimisation phase. |
| `smoke_file_path` | `--smoke-file-path` | `path` | `./.smoke_suite.json` | (pytest plugin) Location of the smoke suite file. |

### 4.3 Termination Logic

The greedy algorithm terminates when **either** condition is met first:

- Accumulated smoke-suite runtime ≥ `time_cap`.
- Achieved coverage ≥ `target_cov` % of total suite branch coverage.

If only one is specified by the user, the other retains its default, acting as a safety bound.

---

## 5. Profiling Phase

### 5.1 Execution

1. Verify pytest is available. Verify `pytest-randomly` is installed (or `--allow-ordered` is set).
2. Invoke pytest with `--cov-context=test` and any user-supplied `pytest_args`.
3. On completion, export coverage data: `coverage json --show-contexts`.
4. Record per-test wall-clock duration using pytest's built-in timing.

### 5.2 Coverage Granularity

| Level | Supported | Notes |
|---|---|---|
| Line coverage | **No** | Considered insufficient. Not offered as an option. |
| Branch coverage | **Yes (default)** | Via `coverage.py` branch mode. |
| MC/DC | Future | Not well-supported in Python tooling today. |
| Bytecode-level | Future | Possible via Atheris integration. |

### 5.3 Intermediate Data Model

The profiling phase produces an intermediate representation (stored as JSON or equivalent) containing:

```
{
  "meta": {
    "timestamp": "...",
    "commit": "...",       // if git is available
    "python_version": "...",
    "coverage_version": "...",
    "command": "...",      // full repro command
    "machine": {
      "os": "Linux",
      "os_version": "6.5.0-44-generic",
      "platform": "Ubuntu 24.04 LTS",
      "architecture": "x86_64",
      "cpu_model": "AMD Ryzen 9 7950X",
      "cpu_cores_physical": 16,
      "cpu_cores_logical": 32,
      "ram_total_mb": 65536,
      "ram_available_mb": 58200,
      "hostname": "ci-runner-04"
    }
  },
  "tests": {
    "test_module::test_func": {
      "duration_s": 0.032,
      "passed": true,
      "branches_covered": ["file.py:12->14", "file.py:20->22", ...]
    },
    ...
  },
  "total_branches": ["file.py:12->14", "file.py:12->16", ...]
}
```

### 5.4 Machine Environment Capture

All profiling output and reports **must** include comprehensive information about the machine the profiling was executed on. This is critical because test durations — and therefore efficiency rankings — are directly tied to the hardware they were measured on. A smoke suite optimised on a 32-core CI runner may have very different timing characteristics when replayed on a developer laptop.

The following fields are captured (best-effort; unavailable fields are omitted, not faked):

| Field | Source | Example |
|---|---|---|
| `os` | `platform.system()` | `Linux` |
| `os_version` | `platform.release()` | `6.5.0-44-generic` |
| `platform` | `platform.platform()` | `Ubuntu 24.04 LTS` |
| `architecture` | `platform.machine()` | `x86_64` |
| `cpu_model` | `/proc/cpuinfo` or equivalent | `AMD Ryzen 9 7950X` |
| `cpu_cores_physical` | `os.cpu_count()` / `psutil` | `16` |
| `cpu_cores_logical` | `os.cpu_count()` | `32` |
| `ram_total_mb` | `psutil.virtual_memory()` | `65536` |
| `ram_available_mb` | `psutil.virtual_memory()` | `58200` |
| `hostname` | `socket.gethostname()` | `ci-runner-04` |

This data is included in both the intermediate profiling data and the final `.smoke_suite.json`, so consumers can assess whether the timing data is still representative of their environment.

### 5.5 Failing Tests

Any test observed to fail during profiling is **hard-excluded** from the optimiser. No exceptions, no overrides. Failing tests are recorded in the intermediate data with `"passed": false` for reporting purposes.

### 5.6 Large-Scale Parsing

For suites with tens of thousands of tests, `coverage json --show-contexts` can produce very large JSON. The parser **must** use streaming/iterative JSON parsing (e.g. `ijson`) rather than loading the entire file into memory.

---

## 6. Optimisation Phase

### 6.1 Algorithm: Greedy Weighted Set Cover

The problem of finding the minimum-time test subset that achieves a coverage target is a variant of the weighted set-cover problem (NP-hard). We use a greedy approximation that is deterministic and provides a known approximation bound.

```
ALGORITHM GreedySmokeSelection

Input:
  tests[]:        list of (test_id, duration, branches_covered_set)
  total_branches:  set of all branches covered by the full suite
  time_cap:       float (seconds)
  target_cov:     float (0.0–1.0, fraction of total_branches)

Pre-processing:
  1. Remove all tests where passed == false
  2. Remove all tests matching exclude_mandatory
  3. Force-include all tests matching include_mandatory
     → add their branches to covered_set
     → add their durations to elapsed_time
  4. Remove included tests from candidate pool

Greedy loop:
  uncovered = total_branches − covered_set
  target_count = ⌈target_cov × |total_branches|⌉

  WHILE |covered_set| < target_count AND elapsed_time < time_cap:
    FOR each candidate test t:
      t.marginal = |t.branches ∩ uncovered|
      t.efficiency = t.marginal / t.duration   (branches per second)

    Pick t* = argmax(efficiency)
      tie-break: higher marginal coverage
      tie-break: shorter duration
      tie-break: alphabetically earlier test_id

    IF t*.marginal == 0:
      BREAK   // no remaining test adds new coverage

    Add t* to smoke_suite
    covered_set ∪= t*.branches
    elapsed_time += t*.duration
    uncovered −= t*.branches

Output: smoke_suite, covered_set, elapsed_time
```

### 6.2 Tie-Breaking Rules

When two or more tests have identical efficiency scores, ties are broken in this order:

1. **Higher marginal branch count** (prefer the test covering more new branches).
2. **Shorter duration** (prefer the faster test).
3. **Alphabetically earlier `test_id`** (deterministic fallback).

### 6.3 Coverage-Equivalent Tests

After optimisation, the tool identifies groups of **coverage-equivalent tests** — tests whose branch-coverage sets are identical. These are reported for informational purposes only. The tool does not recommend removal, as the tests may validate different logical properties despite exercising the same code paths.

### 6.4 Worked Example

```
Tests available (after exclusions):
  test_a: 1.0s, covers {b1, b2, b3}          → eff = 3.0
  test_b: 1.0s, covers {b1, b4}              → eff = 2.0
  test_c: 1.0s, covers {b5}                  → eff = 1.0
  test_d: 1.0s, covers {b3, b5, b6}          → eff = 3.0
  test_e: 10.0s, covers {b1, b2, b3, b4, b5} → eff = 0.5

Total branches: {b1..b6}, time_cap = 5s, target_cov = 100%

Iteration 1: test_a wins (eff 3.0, alpha tiebreak over test_d)
  covered = {b1,b2,b3}, elapsed = 1.0s
  Recalculate: test_b→1.0 (b4), test_c→1.0 (b5), test_d→2.0 (b5,b6), test_e→0.2 (b4,b5)

Iteration 2: test_d wins (eff 2.0)
  covered = {b1,b2,b3,b5,b6}, elapsed = 2.0s

Iteration 3: test_b wins (eff 1.0, b4 only)
  covered = {b1..b6}, elapsed = 3.0s → 100% coverage hit, STOP.

Smoke suite: [test_a, test_d, test_b] — 3s for 100% coverage.
test_e (10s) was never selected despite covering 5 branches.
```

---

## 7. Output

### 7.1 `.smoke_suite.json`

Primary machine-readable output consumed by the pytest plugin.

```json
{
  "version": 1,
  "generated_at": "2026-03-02T10:30:00Z",
  "generator_version": "0.1.0",
  "repro_command": "smoke-optimiser --time-cap 15 --target-cov 80 --output-json .smoke_suite.json",
  "machine": {
    "os": "Linux",
    "os_version": "6.5.0-44-generic",
    "platform": "Ubuntu 24.04 LTS",
    "architecture": "x86_64",
    "cpu_model": "AMD Ryzen 9 7950X",
    "cpu_cores_physical": 16,
    "cpu_cores_logical": 32,
    "ram_total_mb": 65536,
    "ram_available_mb": 58200,
    "hostname": "ci-runner-04"
  },
  "config": {
    "time_cap": 15.0,
    "target_cov": 80.0,
    "include_mandatory": [],
    "exclude_mandatory": ["@pytest.mark.slow"]
  },
  "summary": {
    "total_tests_profiled": 847,
    "tests_passed": 840,
    "tests_failed": 7,
    "total_branches": 12450,
    "smoke_tests_selected": 42,
    "smoke_branches_covered": 9960,
    "smoke_coverage_pct": 80.0,
    "full_suite_runtime_s": 312.5,
    "smoke_suite_runtime_s": 14.8
  },
  "smoke_tests": [
    {
      "test_id": "tests/test_auth.py::test_login_success",
      "duration_s": 0.45,
      "branches_covered": 312,
      "marginal_branches": 312,
      "efficiency": 693.3
    }
  ],
  "coverage_equivalents": [
    {
      "group_id": 1,
      "branch_set_hash": "a1b2c3...",
      "tests": [
        "tests/test_auth.py::test_login_success",
        "tests/test_auth.py::test_login_success_with_mfa"
      ]
    }
  ]
}
```

### 7.2 Human-Readable Report (stdout)

Printed to the console after optimisation:

```
═══════════════════════════════════════════════
  smoke-optimiser results
═══════════════════════════════════════════════

  Machine:      Linux x86_64 — AMD Ryzen 9 7950X
                16 cores / 32 threads, 64 GB RAM
                ci-runner-04

  Profiled:     840 passed, 7 failed (847 total)
  Full suite:   312.5s runtime, 12,450 branches

  Smoke suite:  42 tests selected
  Coverage:     9,960 / 12,450 branches (80.0%)
  Runtime:      14.8s (4.7% of full suite)

  Repro:        smoke-optimiser --time-cap 15 --target-cov 80
                  --output-json .smoke_suite.json

  Saved to:     .smoke_suite.json

  Coverage-equivalent groups: 14 groups (31 tests)
  ⚠ 7 failing tests were excluded.
═══════════════════════════════════════════════
```

### 7.3 Reproducibility

Every report and output file **must** include the full canonical command needed to reproduce the run. This means all effective configuration values are represented — including defaults and values read from `pyproject.toml` — not just the flags the user explicitly typed.

---

## 8. Pytest Plugin (`--smoke`)

### 8.1 Behaviour

- Registered via `pytest11` entry point. Active only when `--smoke` is passed.
- Reads the smoke suite file (default `.smoke_suite.json`, overridable with `--smoke-file-path`).
- During pytest collection, deselects all tests not in `smoke_tests[].test_id`.
- Tests run in whatever order pytest determines (compatible with `pytest-randomly`).

### 8.2 Error Handling

| Condition | Behaviour |
|---|---|
| Smoke suite file not found | Log error, exit with code 1. |
| A test in the smoke suite no longer exists in the collection | Log a warning per missing test, continue with remaining tests. |
| Smoke suite file is malformed JSON | Log error, exit with code 1. |
| `version` field is unsupported | Log error, exit with code 1. |

---

## 9. Test Ordering & Prerequisites

### 9.1 Philosophy

Test suites **must not** depend on execution order. The smoke suite is a subset and must itself be runnable in any order.

### 9.2 `pytest-randomly` Requirement

- If `pytest-randomly` is installed, it is used during profiling (default behaviour).
- If it is **not** installed, `smoke-optimiser` logs a warning and refuses to proceed **unless** `--allow-ordered` is set.
- The rationale: ordering-dependent tests produce unreliable coverage data and unreliable smoke suites.

---

## 10. Operational Requirements

### 10.1 Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success. Smoke suite generated (or smoke run passed). |
| `1` | Failure. Missing inputs, profiling errors, or smoke run had failures. |

### 10.2 Scale & Performance Targets

| Suite Size | Expectation |
|---|---|
| < 1,000 tests | Runs end-to-end in reasonable time on a single machine. |
| 1,000–10,000 tests | Supported. Coverage JSON parsed with `ijson`. |
| 10,000+ tests | Supported with streaming parsing. Greedy algorithm is O(n × b) per iteration where n = tests, b = branches; acceptable for this scale. |

### 10.3 Minimum Versions

| Dependency | Minimum Version | Notes |
|---|---|---|
| Python | 3.12+ | May be lowered if no 3.12-specific features are needed. |
| coverage.py | Latest stable | Required for `--cov-context=test` and branch coverage. |
| pytest | 7.0+ | For stable hook APIs and built-in timing. |
| pytest-cov | 4.0+ | Bridge between pytest and coverage.py. |

### 10.4 State Management

The tool is stateless between runs. The only persisted artefact is `.smoke_suite.json`. There is no database, no daemon, no cache directory. Re-running the tool regenerates everything from scratch.

---

## 11. Future Work (Backburner)

> **Note:** The items below are explicitly out of scope for v1. They are documented here for planning purposes and to inform architectural decisions that should not preclude their future implementation.

### 11.1 Coverage Hole Filling

**Goal:** Identify branches not covered by any existing test and automatically generate tests to cover them.

**Approach (anticipated):**

```
┌──────────────┐     ┌─────────────────┐     ┌──────────────────┐
│  Baseline     │────▶│  Fuzzer          │────▶│  LLM-Driven      │
│  Coverage     │     │  (e.g. Atheris)  │     │  Test Generation  │
│  Analysis     │     │                  │     │                  │
└──────────────┘     └─────────────────┘     └──────────────────┘
      │                      │                        │
      ▼                      ▼                        ▼
  Uncovered             Fuzz tests that           Unit tests
  branch map            hit new branches          targeting holes
```

- **Baseline analysis:** Use the intermediate data from profiling to identify all uncovered branches.
- **Fuzz-powered exploration:** Run an existing fuzzer (e.g. Atheris) against modules with low coverage. Identify fuzz inputs that exercise new branches.
- **LLM test generation:** Provide the source file, the uncovered branch map, and any successful fuzz inputs to a coding LLM with engineered prompts. The LLM generates candidate unit tests. This project's contribution is the prompt engineering and orchestration, not the LLM itself.
- **Validation loop:** Run the generated tests, measure coverage delta, keep only tests that genuinely cover new branches.

### 11.2 Enhanced Coverage Modes

- **MC/DC (Modified Condition/Decision Coverage):** Significantly stronger than branch coverage. Blocked by lack of Python tooling support. Monitor ecosystem developments.
- **Bytecode-level coverage:** Atheris can instrument at the bytecode level. Could provide finer granularity than coverage.py's branch mode.

### 11.3 Additional Test Runners

- `unittest` support.
- `nose2` support.
- Framework-agnostic mode that accepts pre-generated coverage data in a standard format.

---

## 12. Glossary

| Term | Definition |
|---|---|
| **Branch coverage** | A coverage metric where each boolean sub-expression in a decision is evaluated to both true and false. More rigorous than line coverage. |
| **Coverage-equivalent tests** | Two or more tests whose sets of covered branches are identical. |
| **Efficiency (marginal)** | `new_branches_covered / test_duration` — the primary ranking metric for the greedy algorithm. |
| **MC/DC** | Modified Condition/Decision Coverage. An advanced coverage criterion used in safety-critical systems (e.g. DO-178C). |
| **Smoke suite** | A small, fast subset of the full test suite designed for rapid feedback. |
| **Time cap** | The maximum allowed wall-clock runtime for the smoke suite. |
| **Target coverage** | The minimum percentage of the full suite's branch coverage the smoke suite should achieve. |

---

## Appendix A: Example `pyproject.toml` Configuration

```toml
[tool.smoke_optimiser]
time_cap = 15.0
target_cov = 80.0
include_mandatory = ["@pytest.mark.smoke"]
exclude_mandatory = ["@pytest.mark.slow", "@pytest.mark.integration"]
pytest_args = "--timeout=30"
output_json = ".smoke_suite.json"
allow_ordered = false
```

## Appendix B: Example CLI Invocations

```bash
# Full run with defaults
smoke-optimiser

# Profile only (eg on CI, save intermediate data for later)
smoke-optimiser --profile-only

# Optimise from existing profile data, custom targets
smoke-optimiser --optimise-only --time-cap 10 --target-cov 90

# Run the smoke suite via pytest
pytest --smoke

# Smoke suite file in a non-default location
pytest --smoke --smoke-file-path=build/.smoke_suite.json
```

## Appendix C: Decision Log

| Decision | Rationale |
|---|---|
| Branch coverage only (no line coverage) | Line coverage is too coarse to meaningfully differentiate test value. |
| Greedy algorithm over exact solver | Exact weighted set cover is NP-hard. Greedy provides a ln(n)+1 approximation ratio, which is sufficient for this use case. |
| Alphabetical tie-breaking | Imperfect but deterministic. Will evaluate in practice and may adopt test-stability or historical-failure-rate tie-breaking in future. |
| `pytest-randomly` strongly recommended | Ordering-dependent suites produce unreliable coverage data. Enforcing randomisation surfaces hidden dependencies early. |
| Stateless design | Keeps the tool simple, composable, and CI-friendly. No daemon, no database, no cache invalidation complexity. |
| Streaming JSON parser for large suites | `coverage json --show-contexts` can produce multi-GB files for large suites. `ijson` keeps memory usage bounded. |
| Machine environment in all outputs | Test durations are hardware-dependent. Recording the profiling machine's specs lets consumers judge whether timing data is representative of their environment. |
