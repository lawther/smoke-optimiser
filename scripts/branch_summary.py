"""Print a branch coverage summary from a coverage.py JSON report."""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel


class _FileSummary(BaseModel):
    num_branches: int
    covered_branches: int
    num_partial_branches: int
    missing_branches: int


class _FileData(BaseModel):
    summary: _FileSummary


class _Totals(BaseModel):
    num_branches: int
    covered_branches: int
    num_partial_branches: int
    missing_branches: int


class _Report(BaseModel):
    totals: _Totals
    files: dict[str, _FileData]


def _pct(s: _FileSummary | _Totals) -> float:
    if s.num_branches == 0:
        return 100.0
    return s.covered_branches / s.num_branches * 100


def main() -> None:
    expected_args = 2
    if len(sys.argv) != expected_args:
        sys.exit("Usage: branch_summary.py <coverage.json>")

    report = _Report.model_validate_json(Path(sys.argv[1]).read_bytes())

    rows = [(name, f.summary) for name, f in sorted(report.files.items()) if f.summary.num_branches > 0]

    name_width = max((len(name) for name, _ in rows), default=4)
    name_width = max(name_width, len("File"))

    header = f"{'File':<{name_width}}  {'Exits':>8}  {'Hit':>7}  {'Partial':>7}  {'Missed':>6}  {'Cover':>6}"
    separator = "-" * len(header)
    print()
    print(header)
    print(separator)
    for name, s in rows:
        print(
            f"{name:<{name_width}}  {s.num_branches:>8}  {s.covered_branches:>7}  "
            f"{s.num_partial_branches:>7}  {s.missing_branches:>6}  "
            f"{_pct(s):>5.0f}%"
        )

    t = report.totals
    print(separator)
    print(
        f"{'TOTAL':<{name_width}}  {t.num_branches:>8}  {t.covered_branches:>7}  "
        f"{t.num_partial_branches:>7}  {t.missing_branches:>6}  "
        f"{_pct(t):>5.0f}%"
    )


if __name__ == "__main__":
    main()
