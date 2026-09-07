import json
from pathlib import Path

import pytest


def test_plugin_options_registered(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure smoke_optimiser is importable by subprocess
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--help")
    result.stdout.fnmatch_lines(
        [
            "*smoke-optimiser:*",
            "*--smoke *Run only the tests in the smoke suite.*",
            "*--smoke-file-path=SMOKE_FILE_PATH*",
            "*--downwind *Run only the tests downwind of the changed files.*",
            "*--downwind-file-path=DOWNWIND_FILE_PATH*",
        ],
    )


def test_plugin_load_suite_success(pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
            "full_suite_branches_covered": 10,
            "smoke_tests_selected": 1,
            "smoke_branches_covered": 10,
            "smoke_coverage_pct": 100.0,
            "full_suite_runtime_s": 1.0,
            "smoke_suite_runtime_s": 0.1,
        },
        "smoke_tests": [
            {
                "test_id": "test_dummy.py::test_load",
                "duration_s": 0.1,
                "branches_covered": 5,
                "marginal_branches": 5,
                "efficiency": 50.0,
            },
        ],
        "coverage_equivalents": [],
    }
    suite_file = tmp_path / "smoke.json"
    suite_file.write_text(json.dumps(suite_data))

    pytester.makepyfile(
        test_dummy="""
        import pytest
        def test_load(pytestconfig):
            pass
        """,
    )
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--smoke", f"--smoke-file-path={suite_file}")
    result.assert_outcomes(passed=1)


def test_plugin_load_suite_not_found(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--smoke", "--smoke-file-path=nonexistent.json")
    assert result.ret == 1
    result.stderr.fnmatch_lines(["*smoke-optimiser: ❌ Error: smoke suite file not found: nonexistent.json*"])


def test_plugin_load_suite_malformed(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite_file = tmp_path / "malformed.json"
    suite_file.write_text("{invalid json}")

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--smoke", f"--smoke-file-path={suite_file}")
    assert result.ret == 1
    result.stderr.fnmatch_lines(["*smoke-optimiser: ❌ Error: invalid smoke suite file:*"])


def test_plugin_load_suite_unsupported_version(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            "full_suite_branches_covered": 10,
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

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--smoke", f"--smoke-file-path={suite_file}")
    assert result.ret == 1
    result.stderr.fnmatch_lines(["*smoke-optimiser: ❌ Error: unsupported smoke suite version 999*"])


def test_plugin_filtering(pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite_data = {
        "version": 1,
        "generated_at": "2026-03-02T10:30:00Z",
        "repro_command": "...",
        "machine": {},
        "config": {},
        "summary": {
            "total_tests_profiled": 2,
            "tests_passed": 2,
            "tests_failed": 0,
            "total_branches": 10,
            "full_suite_branches_covered": 10,
            "smoke_tests_selected": 1,
            "smoke_branches_covered": 5,
            "smoke_coverage_pct": 50.0,
            "full_suite_runtime_s": 1.0,
            "smoke_suite_runtime_s": 0.1,
        },
        "smoke_tests": [
            {
                "test_id": "test_app.py::test_smoke",
                "duration_s": 0.1,
                "branches_covered": 5,
                "marginal_branches": 5,
                "efficiency": 50.0,
            },
        ],
        "coverage_equivalents": [],
    }
    suite_file = tmp_path / "smoke.json"
    suite_file.write_text(json.dumps(suite_data))

    pytester.makepyfile(
        test_app="""
        def test_smoke(): pass
        def test_other(): pass
        """,
    )

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--smoke", f"--smoke-file-path={suite_file}")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*1 passed, 1 deselected*"])


def test_plugin_missing_test_warning(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            "full_suite_branches_covered": 10,
            "smoke_tests_selected": 1,
            "smoke_branches_covered": 5,
            "smoke_coverage_pct": 50.0,
            "full_suite_runtime_s": 1.0,
            "smoke_suite_runtime_s": 0.1,
        },
        "smoke_tests": [
            {
                "test_id": "test_app.py::test_missing",
                "duration_s": 0.1,
                "branches_covered": 5,
                "marginal_branches": 5,
                "efficiency": 50.0,
            },
        ],
        "coverage_equivalents": [],
    }
    suite_file = tmp_path / "smoke.json"
    suite_file.write_text(json.dumps(suite_data))

    pytester.makepyfile(test_app="def test_exists(): pass")

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--smoke", f"--smoke-file-path={suite_file}")
    result.stdout.fnmatch_lines(
        [
            "*smoke-optimiser: ⚠️ Warning: smoke test not found in collection: test_app.py::test_missing*",
            "*1 deselected*",
        ],
    )


def test_plugin_no_smoke_flag(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    pytester.makepyfile(test_app="def test_1(): pass")
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*deselected*")


def test_plugin_report_header(pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite_data = {
        "version": 1,
        "generated_at": "2026-03-02T10:30:00Z",
        "repro_command": "...",
        "machine": {"hostname": "test-machine"},
        "config": {},
        "summary": {
            "total_tests_profiled": 1,
            "tests_passed": 1,
            "tests_failed": 0,
            "total_branches": 10,
            "full_suite_branches_covered": 10,
            "smoke_tests_selected": 1,
            "smoke_branches_covered": 8,
            "smoke_coverage_pct": 80.0,
            "full_suite_runtime_s": 1.0,
            "smoke_suite_runtime_s": 0.1,
        },
        "smoke_tests": [
            {
                "test_id": "test_app.py::test_1",
                "duration_s": 0.1,
                "branches_covered": 8,
                "marginal_branches": 8,
                "efficiency": 80.0,
            },
        ],
        "coverage_equivalents": [],
    }
    suite_file = tmp_path / "smoke.json"
    suite_file.write_text(json.dumps(suite_data))

    pytester.makepyfile(test_app="def test_1(): pass")

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--smoke", f"--smoke-file-path={suite_file}")
    result.stdout.fnmatch_lines(["*smoke-optimiser: running smoke suite from *smoke.json *1 tests, 80.0% coverage*"])


def _downwind_data(node_ids: list[str], blind_spots: list[dict] | None = None) -> dict:
    return {
        "version": 1,
        "generated_at": "2026-09-07T09:00:00Z",
        "changed_files": ["smoke_optimiser/downwind/maps.py"],
        "node_ids": node_ids,
        "profile": {"commit": "abcdef", "timestamp": "2026-09-07T09:00:00Z"},
        "blind_spots": blind_spots or [],
    }


def test_plugin_downwind_load_not_found(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--downwind", "--downwind-file-path=nonexistent.json")
    assert result.ret == 1
    result.stderr.fnmatch_lines(["*smoke-optimiser: ❌ Error: downwind selection file not found: nonexistent.json*"])


def test_plugin_downwind_load_malformed(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite_file = tmp_path / "malformed.json"
    suite_file.write_text("{invalid json}")

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--downwind", f"--downwind-file-path={suite_file}")
    assert result.ret == 1
    result.stderr.fnmatch_lines(["*smoke-optimiser: ❌ Error: invalid downwind selection file:*"])


def test_plugin_downwind_load_unsupported_version(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _downwind_data([])
    data["version"] = 999
    suite_file = tmp_path / "version999.json"
    suite_file.write_text(json.dumps(data))

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--downwind", f"--downwind-file-path={suite_file}")
    assert result.ret == 1
    result.stderr.fnmatch_lines(["*smoke-optimiser: ❌ Error: unsupported downwind selection version 999*"])


def test_plugin_downwind_filtering(pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite_file = tmp_path / "downwind.json"
    suite_file.write_text(json.dumps(_downwind_data(["test_app.py::test_downwind"])))

    pytester.makepyfile(
        test_app="""
        def test_downwind(): pass
        def test_other(): pass
        """,
    )

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--downwind", f"--downwind-file-path={suite_file}")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*1 passed, 1 deselected*"])


def test_plugin_downwind_missing_test_warning(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite_file = tmp_path / "downwind.json"
    suite_file.write_text(json.dumps(_downwind_data(["test_app.py::test_missing"])))

    pytester.makepyfile(test_app="def test_exists(): pass")

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--downwind", f"--downwind-file-path={suite_file}")
    result.stdout.fnmatch_lines(
        [
            "*smoke-optimiser: ⚠️ Warning: downwind test not found in collection*: test_app.py::test_missing*",
            "*1 deselected*",
        ],
    )


def test_plugin_downwind_blind_spot_runs_full_suite(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite_file = tmp_path / "downwind.json"
    suite_file.write_text(
        json.dumps(
            _downwind_data(
                [],
                blind_spots=[{"reason": "unknown_path", "file": "smoke_optimiser/new_module.py"}],
            ),
        ),
    )

    pytester.makepyfile(
        test_app="""
        def test_one(): pass
        def test_two(): pass
        """,
    )

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--downwind", f"--downwind-file-path={suite_file}")
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*deselected*")


def test_plugin_downwind_report_header_selected(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite_file = tmp_path / "downwind.json"
    suite_file.write_text(json.dumps(_downwind_data(["test_app.py::test_1"])))

    pytester.makepyfile(test_app="def test_1(): pass")

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--downwind", f"--downwind-file-path={suite_file}")
    result.stdout.fnmatch_lines(
        ["*smoke-optimiser: running downwind suite from *downwind.json *1 tests downwind of 1 changed files*"],
    )
    result.stdout.no_fnmatch_line("*coverage*")


def test_plugin_downwind_report_header_blind_spot(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite_file = tmp_path / "downwind.json"
    suite_file.write_text(
        json.dumps(
            _downwind_data(
                [],
                blind_spots=[{"reason": "resolution_errors", "resolution_errors": 3}],
            ),
        ),
    )

    pytester.makepyfile(test_app="def test_1(): pass")

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess("--downwind", f"--downwind-file-path={suite_file}")
    result.stdout.fnmatch_lines(
        ["*smoke-optimiser: downwind selection from *downwind.json* hit 1 blind spots across*running full suite*"],
    )
    result.stdout.no_fnmatch_line("*coverage*")


def test_plugin_smoke_and_downwind_together_is_refused(
    pytester: pytest.Pytester,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing both flags must fail loudly rather than silently intersecting the two selections.

    The smoke file path is deliberately absent while the downwind one is valid: if
    the guard ran after the loaders we would see "smoke suite file not found"
    instead, so this also pins the guard to the top of pytest_configure.
    """
    downwind_file = tmp_path / "downwind.json"
    downwind_file.write_text(json.dumps(_downwind_data(["test_app.py::test_1"])))

    pytester.makepyfile(
        test_app="""
        def test_1(): pass
        def test_2(): pass
        """,
    )

    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    result = pytester.runpytest_subprocess(
        "--smoke",
        "--smoke-file-path=nonexistent.json",
        "--downwind",
        f"--downwind-file-path={downwind_file}",
    )

    assert result.ret == 1
    result.stderr.fnmatch_lines(
        ["*smoke-optimiser: ❌ Error: --smoke and --downwind cannot be used together.*"],
    )
    # The smoke file is deliberately absent: if the guard ran too late we would see
    # the "smoke suite file not found" error instead of the mutual-exclusion one.
    result.stderr.no_fnmatch_line("*smoke suite file not found*")
    # Nothing may run: the refusal happens at configure time, before collection.
    result.stdout.no_fnmatch_line("*passed*")
