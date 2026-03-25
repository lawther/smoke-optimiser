# smoke-optimiser

`smoke-optimiser` is a tool and pytest plugin that analyses your test suite to produce a minimal **smoke suite** — a subset of tests that delivers maximum code coverage in minimum wall-clock time.

It helps you find the "bang for buck" sweet spot: for example, achieving 80% of your total branch coverage in only 5% of the total runtime.

## Installation

Add `smoke-optimiser` as a development dependency in your project:

```bash
uv add --dev smoke-optimiser
```

Or with pip:

```bash
pip install --dev smoke-optimiser
```

This will make the `smoke-optimiser` command available in your environment and register the pytest plugin automatically.

## Quickstart

1. **Generate the smoke suite**:
   Run the optimiser in your project root. It will automatically detect your source code and profile your tests.
   ```bash
   uv run smoke-optimiser
   ```

2. **Run the smoke suite**:
   Use the `--smoke` flag with pytest to run only the selected high-value tests.
   ```bash
   uv run pytest --smoke
   ```

## Common Usages

### Custom Efficiency Targets
By default, the tool tries to get maximum coverage within a 15-second time cap. You can tighten these bounds:
```bash
# Aim for 80% coverage, but stop if it takes longer than 5 seconds
uv run smoke-optimiser --target-cov=80 --time-cap=5
```

### Stabilising Timing Data
Test execution times can vary. Use `--iterations` to run the suite multiple times and average the results for a more stable smoke suite:
```bash
uv run smoke-optimiser --iterations=3
```

### Mandatory Inclusion/Exclusion
Force certain tests (or markers) to be included or excluded from the smoke suite:
```bash
# Always include authentication tests, but exclude anything marked as 'slow'
uv run smoke-optimiser --include="tests/test_auth.py" --exclude="@pytest.mark.slow"
```
*Multiple items can be separated by commas.*

## Command-line Arguments

### `smoke-optimiser` (Generator)

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
| `--profile-only` | Run only the profiling phase and save intermediate data. | `False` |
| `--optimise-only` | Run only the optimisation phase using existing profile data. | `False` |
| `--allow-ordered` | Suppress warning when `pytest-randomly` is not installed. | `False` |

### `pytest` (Plugin)

| Argument | Description | Default |
| :--- | :--- | :--- |
| `--smoke` | Activates the plugin; filters collection to the smoke suite. | `False` |
| `--smoke-file-path` | Path to the smoke suite JSON file to use. | `.smoke_suite.json` |

## How it works

1. **Profiling**: It runs your suite with `pytest-cov` and a custom hook to map every single branch execution to specific tests.
2. **Analysis**: It calculates the "efficiency" of every test (New Branches Covered / Duration).
3. **Greedy Selection**: It iteratively picks the most efficient test until your coverage target or time cap is reached.
4. **Redundancy Reporting**: It identifies "Coverage-equivalent groups" — sets of tests that cover the exact same logic.
