import json
from pathlib import Path

import pytest


def test_plugin_options_registered(pytester: pytest.Pytester) -> None:
    result = pytester.runpytest("--help")
    result.stdout.fnmatch_lines(
        [
            "*smoke-optimiser:*",
            "*--smoke *Run only the tests in the smoke suite.*",
            "*--smoke-file-path=SMOKE_FILE_PATH*",
        ]
    )


def test_plugin_load_suite_success(pytester: pytest.Pytester, tmp_path: Path) -> None:
    suite_data = {
        "version": 1,
        "generated_at": "2026-03-02T10:30:00Z",
        "repro_command": "...",
        "machine": {},
        "config": {},
        "summary": {
            "total_tests_profiled": 1,
            "tests_passed": 1,
            "tests_failed": 0,
            "total_branches": 10,
            "smoke_tests_selected": 1,
            "smoke_branches_covered": 10,
            "smoke_coverage_pct": 100.0,
            "full_suite_runtime_s": 1.0,
            "smoke_suite_runtime_s": 0.1,
        },
        "smoke_tests": [],
        "coverage_equivalents": [],
    }
    suite_file = tmp_path / "smoke.json"
    suite_file.write_text(json.dumps(suite_data))

    pytester.makepyfile(
        test_dummy="""
        import pytest
        from smoke_optimiser.plugin import _load_smoke_suite
        def test_load(pytestconfig):
            suite = _load_smoke_suite(pytestconfig)
            assert suite is not None
            assert suite.version == 1
        """
    )
    result = pytester.runpytest("--smoke", f"--smoke-file-path={suite_file}")
    result.assert_outcomes(passed=1)


def test_plugin_load_suite_not_found(pytester: pytest.Pytester) -> None:
    result = pytester.runpytest("--smoke", "--smoke-file-path=nonexistent.json")
    assert result.ret == 1
    result.stderr.fnmatch_lines(["*smoke-optimiser: smoke suite file not found: nonexistent.json*"])


def test_plugin_load_suite_malformed(pytester: pytest.Pytester, tmp_path: Path) -> None:
    suite_file = tmp_path / "malformed.json"
    suite_file.write_text("{invalid json}")

    result = pytester.runpytest("--smoke", f"--smoke-file-path={suite_file}")
    assert result.ret == 1
    result.stderr.fnmatch_lines(["*smoke-optimiser: invalid smoke suite file:*"])


def test_plugin_load_suite_unsupported_version(pytester: pytest.Pytester, tmp_path: Path) -> None:
    suite_data = {
        "version": 999,
        "generated_at": "2026-03-02T10:30:00Z",
        "repro_command": "...",
        "machine": {},
        "config": {},
        "summary": {
            "total_tests_profiled": 1,
            "tests_passed": 1,
            "tests_failed": 0,
            "total_branches": 10,
            "smoke_tests_selected": 1,
            "smoke_branches_covered": 10,
            "smoke_coverage_pct": 100.0,
            "full_suite_runtime_s": 1.0,
            "smoke_suite_runtime_s": 0.1,
        },
        "smoke_tests": [],
        "coverage_equivalents": [],
    }
    suite_file = tmp_path / "version999.json"
    suite_file.write_text(json.dumps(suite_data))

    result = pytester.runpytest("--smoke", f"--smoke-file-path={suite_file}")
    assert result.ret == 1
    result.stderr.fnmatch_lines(["*smoke-optimiser: unsupported smoke suite version 999*"])
