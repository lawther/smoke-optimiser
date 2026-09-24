"""Analyse and list source files in the project to evaluate AI agent readiness."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import NamedTuple

import tiktoken
from rich.console import Console
from rich.table import Table

_ENCODING = tiktoken.get_encoding("o200k_base")

BYTES_IN_KIB = 1024
BYTES_IN_KIB_FLOAT = 1024.0

THRESHOLD_AMBER = 300
THRESHOLD_RED = 600
THRESHOLD_CRITICAL = 1000


class Language(Enum):
    PYTHON = "Python"
    TYPESCRIPT = "TypeScript"
    SWIFT = "Swift"
    RUST = "Rust"
    HTML = "HTML"
    CSS = "CSS"
    JAVASCRIPT = "JavaScript"
    SHELL = "Shell"
    CUSTOM = "Custom"
    UNKNOWN = "Unknown"


class SizeZone(Enum):
    GREEN = "Green"
    AMBER = "Amber"
    RED = "Red"
    CRITICAL = "Critical"


class FailThreshold(Enum):
    NONE = "none"
    CRITICAL = "critical"
    RED = "red"
    AMBER = "amber"

    def includes_zone(self, zone: SizeZone) -> bool:
        if self == FailThreshold.CRITICAL:
            return zone == SizeZone.CRITICAL
        if self == FailThreshold.RED:
            return zone in (SizeZone.RED, SizeZone.CRITICAL)
        if self == FailThreshold.AMBER:
            return zone in (SizeZone.AMBER, SizeZone.RED, SizeZone.CRITICAL)
        return False


class FileScope(Enum):
    ALL_TRACKED = "all"
    STAGED_ONLY = "staged"


class SourceFile(NamedTuple):
    path: Path
    line_count: int
    size_bytes: int
    token_count: int
    language: Language
    zone: SizeZone


class ProjectSummary(NamedTuple):
    total_files: int
    total_lines: int
    total_bytes: int
    total_tokens: int
    language_counts: dict[Language, int]
    language_lines: dict[Language, int]
    language_bytes: dict[Language, int]
    language_tokens: dict[Language, int]
    zone_counts: dict[SizeZone, int]


class CheckResult(NamedTuple):
    passed: bool
    threshold: FailThreshold
    offending_files: list[SourceFile]


EXT_TO_LANG: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".swift": Language.SWIFT,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".html": Language.HTML,
    ".css": Language.CSS,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".rs": Language.RUST,
}


def get_language(path: Path, extra_exts: set[str]) -> Language:
    ext = path.suffix.lower()
    if (lang := EXT_TO_LANG.get(ext)) is not None:
        return lang
    if path.name == "pre-commit" or ext == ".sh":
        return Language.SHELL
    if ext in extra_exts:
        return Language.CUSTOM
    return Language.UNKNOWN


def get_size_zone(line_count: int) -> SizeZone:
    if line_count < THRESHOLD_AMBER:
        return SizeZone.GREEN
    if line_count < THRESHOLD_RED:
        return SizeZone.AMBER
    if line_count < THRESHOLD_CRITICAL:
        return SizeZone.RED
    return SizeZone.CRITICAL


def count_non_blank_lines(file_path: Path) -> int:
    try:
        with file_path.open(encoding="utf-8", errors="ignore") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def count_tokens(file_path: Path) -> int:
    try:
        with file_path.open(encoding="utf-8", errors="ignore") as f:
            return len(_ENCODING.encode(f.read()))
    except OSError:
        return 0


def git_executable() -> str:
    """Absolute path to git, so subprocess never resolves a partial path (ruff S607)."""
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git not found on PATH")
    return git


def get_tracked_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            [git_executable(), "ls-files"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
        )
        return [root / line for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.SubprocessError, FileNotFoundError):
        return walk_directory(root)


def get_staged_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            [git_executable(), "diff", "--cached", "--name-only", "--diff-filter=d"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
        )
        return [root / line for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.SubprocessError, FileNotFoundError):
        return []


def walk_directory(root: Path) -> list[Path]:
    ignored_dirs = {".git", ".venv", "node_modules", ".pytest_cache", ".ruff_cache", "__pycache__"}
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file():
            if any(part in ignored_dirs for part in path.parts):
                continue
            files.append(path)
    return files


def format_bytes(size: int) -> str:
    """Format bytes into a human-readable string in Australian English style."""
    if size < BYTES_IN_KIB:
        return f"{size} B"
    for unit in ("KB", "MB", "GB"):
        size_val = size / BYTES_IN_KIB_FLOAT
        if size_val < BYTES_IN_KIB_FLOAT:
            return f"{size_val:.1f} {unit}"
        size = int(size_val)
    return f"{size} GB"


def analyse_project(
    root: Path,
    extra_exts: set[str],
    *,
    include_all: bool,
    scope: FileScope,
) -> list[SourceFile]:
    files = get_staged_files(root) if scope == FileScope.STAGED_ONLY else get_tracked_files(root)

    source_files: list[SourceFile] = []
    for f in files:
        lang = get_language(f, extra_exts)
        if not include_all and lang == Language.UNKNOWN:
            continue
        line_count = count_non_blank_lines(f)
        token_count = count_tokens(f)
        try:
            size_bytes = f.stat().st_size
        except OSError:
            size_bytes = 0
        rel_path = f.relative_to(root) if f.is_absolute() else f
        source_files.append(
            SourceFile(
                path=rel_path,
                line_count=line_count,
                size_bytes=size_bytes,
                token_count=token_count,
                language=lang,
                zone=get_size_zone(line_count),
            )
        )
    return source_files


def calculate_summary(files: list[SourceFile]) -> ProjectSummary:
    total_files = len(files)
    total_lines = sum(f.line_count for f in files)
    total_bytes = sum(f.size_bytes for f in files)
    total_tokens = sum(f.token_count for f in files)

    lang_counts: dict[Language, int] = dict.fromkeys(Language, 0)
    lang_lines: dict[Language, int] = dict.fromkeys(Language, 0)
    lang_bytes: dict[Language, int] = dict.fromkeys(Language, 0)
    lang_tokens: dict[Language, int] = dict.fromkeys(Language, 0)

    zone_counts: dict[SizeZone, int] = dict.fromkeys(SizeZone, 0)

    for f in files:
        lang_counts[f.language] += 1
        lang_lines[f.language] += f.line_count
        lang_bytes[f.language] += f.size_bytes
        lang_tokens[f.language] += f.token_count
        zone_counts[f.zone] += 1

    return ProjectSummary(
        total_files=total_files,
        total_lines=total_lines,
        total_bytes=total_bytes,
        total_tokens=total_tokens,
        language_counts=lang_counts,
        language_lines=lang_lines,
        language_bytes=lang_bytes,
        language_tokens=lang_tokens,
        zone_counts=zone_counts,
    )


ZONE_TO_STYLE: dict[SizeZone, str] = {
    SizeZone.GREEN: "green",
    SizeZone.AMBER: "yellow",
    SizeZone.RED: "red",
    SizeZone.CRITICAL: "bold red",
}

ZONE_TO_DESC: dict[SizeZone, str] = {
    SizeZone.GREEN: f"< {THRESHOLD_AMBER} lines (Optimal for AI agents)",
    SizeZone.AMBER: f"{THRESHOLD_AMBER} - {THRESHOLD_RED} lines (Manageable, use surgical edits)",
    SizeZone.RED: f"{THRESHOLD_RED} - {THRESHOLD_CRITICAL} lines (Challenging, split recommended)",
    SizeZone.CRITICAL: f"> {THRESHOLD_CRITICAL} lines (Dangerous, refactor urgently)",
}


def run_check(files: list[SourceFile], threshold: FailThreshold) -> CheckResult:
    offending = [f for f in files if threshold.includes_zone(f.zone)]
    offending_sorted = sorted(offending, key=lambda f: f.line_count, reverse=True)
    return CheckResult(
        passed=len(offending_sorted) == 0,
        threshold=threshold,
        offending_files=offending_sorted,
    )


def display_check_results(result: CheckResult) -> None:
    console = Console(stderr=True)
    if result.passed:
        console.print(f"[bold green]✔︎ AI readiness check passed (threshold: {result.threshold.value})[/bold green]")
        return

    count = len(result.offending_files)
    file_noun = "file" if count == 1 else "files"
    t_val = result.threshold.value
    console.print(
        f"[bold red]❌ AI readiness check failed:[/bold red] {count} {file_noun} "
        f"exceed allowable threshold '{t_val}'.\n"
    )
    console.print("[bold cyan]Offending files requiring refactoring:[/bold cyan]")
    for f in result.offending_files:
        style = ZONE_TO_STYLE[f.zone]
        console.print(
            f"  • [bold]{f.path}[/bold]: {f.line_count:,} non-blank lines (Zone: [{style}]{f.zone.value}[/{style}])"
        )

    console.print("\n[bold yellow]Instruction for AI agent:[/bold yellow]")
    console.print(
        f"Please use the skill [bold magenta]/refactor-file[/bold magenta] to refactor the above {file_noun} into "
        "smaller, cohesive modules."
    )


def display_results(
    files: list[SourceFile],
    summary: ProjectSummary,
    *,
    sort_ascending: bool,
) -> None:
    console = Console()

    sorted_files = sorted(files, key=lambda f: f.line_count, reverse=not sort_ascending)

    table = Table(title="Project Source Files", show_header=True, header_style="bold magenta")
    table.add_column("File Path", style="dim")
    table.add_column("Language", style="cyan")
    table.add_column("Lines", justify="right", style="green")
    table.add_column("Tokens", justify="right", style="yellow")
    table.add_column("Size", justify="right", style="blue")
    table.add_column("Zone", justify="center")

    for f in sorted_files:
        style = ZONE_TO_STYLE[f.zone]
        table.add_row(
            str(f.path),
            f.language.value,
            f"{f.line_count:,}",
            f"{f.token_count:,}",
            format_bytes(f.size_bytes),
            f"[{style}]{f.zone.value}[/{style}]",
        )

    console.print(table)
    console.print()

    summary_table = Table(title="Summary by Language", show_header=True, header_style="bold magenta")
    summary_table.add_column("Language", style="cyan")
    summary_table.add_column("Files", justify="right", style="dim")
    summary_table.add_column("Lines (non-blank)", justify="right", style="green")
    summary_table.add_column("Tokens (est.)", justify="right", style="yellow")
    summary_table.add_column("Size", justify="right", style="blue")

    sorted_langs = sorted(
        [lang for lang in Language if summary.language_counts[lang] > 0],
        key=lambda lang_key: summary.language_lines[lang_key],
        reverse=True,
    )

    for lang in sorted_langs:
        summary_table.add_row(
            lang.value,
            f"{summary.language_counts[lang]:,}",
            f"{summary.language_lines[lang]:,}",
            f"{summary.language_tokens[lang]:,}",
            format_bytes(summary.language_bytes[lang]),
        )

    summary_table.add_row(
        "Total",
        f"{summary.total_files:,}",
        f"{summary.total_lines:,}",
        f"{summary.total_tokens:,}",
        format_bytes(summary.total_bytes),
        style="bold white",
    )

    console.print(summary_table)
    console.print()

    zone_table = Table(title="AI Agent Readiness Breakdown", show_header=True, header_style="bold magenta")
    zone_table.add_column("Readiness Zone", style="cyan")
    zone_table.add_column("Files", justify="right", style="dim")
    zone_table.add_column("Threshold & Recommendation", style="white")

    for zone in SizeZone:
        count = summary.zone_counts[zone]
        style = ZONE_TO_STYLE[zone]
        desc = ZONE_TO_DESC[zone]
        zone_table.add_row(
            f"[{style}]{zone.value}[/{style}]",
            f"{count:,}",
            desc,
        )

    console.print(zone_table)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyse project source files by line count.")
    parser.add_argument(
        "--descending",
        action="store_true",
        help="Sort files in descending order of line count (default: ascending).",
    )
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="Include all tracked files, not just recognised source files.",
    )
    parser.add_argument(
        "--extensions",
        type=str,
        default="",
        help="Comma-separated list of additional file extensions to include (e.g. .json,.toml).",
    )
    parser.add_argument(
        "--fail-on",
        nargs="?",
        const=FailThreshold.CRITICAL.value,
        default=FailThreshold.NONE.value,
        choices=[t.value for t in FailThreshold if t != FailThreshold.NONE],
        help=(
            "Run in check mode and fail (exit code 1) if any source file reaches or exceeds the specified "
            "size threshold (critical, red, amber). Defaults to critical if flag is present without a value."
        ),
    )
    parser.add_argument(
        "--staged-only",
        action="store_true",
        help="Evaluate only git staged files instead of all tracked files in the repository.",
    )

    args = parser.parse_args()
    root = Path.cwd()

    extra_exts = {ext.strip().lower() for ext in args.extensions.split(",") if ext.strip()}
    scope = FileScope.STAGED_ONLY if args.staged_only else FileScope.ALL_TRACKED
    files = analyse_project(root, extra_exts, include_all=args.all_files, scope=scope)

    threshold = FailThreshold(args.fail_on)
    if threshold != FailThreshold.NONE:
        check_result = run_check(files, threshold)
        display_check_results(check_result)
        if not check_result.passed:
            sys.exit(1)
        return

    summary = calculate_summary(files)
    display_results(files, summary, sort_ascending=not args.descending)


if __name__ == "__main__":
    main()
