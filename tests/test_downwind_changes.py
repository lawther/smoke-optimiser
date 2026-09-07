import subprocess
from pathlib import Path

import pytest

from smoke_optimiser.downwind.changes import (
    ChangedFile,
    ChangeKind,
    GitStatusError,
    changed_files,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)  # noqa: S603, S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    return root


def _commit(repo: Path, path: str, content: str) -> None:
    file_path = repo / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", f"add {path}")


def test_unstaged_edit_is_modified(repo: Path) -> None:
    _commit(repo, "a.py", "one\n")
    (repo / "a.py").write_text("two\n")

    assert changed_files(repo) == frozenset({ChangedFile("a.py", ChangeKind.MODIFIED)})


def test_staged_edit_is_modified(repo: Path) -> None:
    _commit(repo, "a.py", "one\n")
    (repo / "a.py").write_text("two\n")
    _git(repo, "add", "a.py")

    assert changed_files(repo) == frozenset({ChangedFile("a.py", ChangeKind.MODIFIED)})


def test_untracked_file_is_added(repo: Path) -> None:
    _commit(repo, "a.py", "one\n")
    (repo / "new.py").write_text("new\n")

    assert changed_files(repo) == frozenset({ChangedFile("new.py", ChangeKind.ADDED)})


def test_staged_new_file_is_added(repo: Path) -> None:
    _commit(repo, "a.py", "one\n")
    (repo / "new.py").write_text("new\n")
    _git(repo, "add", "new.py")

    assert changed_files(repo) == frozenset({ChangedFile("new.py", ChangeKind.ADDED)})


def test_unstaged_delete_is_deleted(repo: Path) -> None:
    _commit(repo, "a.py", "one\n")
    (repo / "a.py").unlink()

    result = changed_files(repo)

    assert result == frozenset({ChangedFile("a.py", ChangeKind.DELETED)})


def test_staged_delete_is_deleted(repo: Path) -> None:
    _commit(repo, "a.py", "one\n")
    _git(repo, "rm", "-q", "a.py")

    result = changed_files(repo)

    assert result == frozenset({ChangedFile("a.py", ChangeKind.DELETED)})


def test_deleted_file_is_never_filtered_for_not_existing_on_disk(repo: Path) -> None:
    _commit(repo, "a.py", "one\n")
    (repo / "a.py").unlink()

    result = changed_files(repo)

    assert not (repo / "a.py").exists()
    assert ChangedFile("a.py", ChangeKind.DELETED) in result


def test_renamed_file_appears_once_at_its_old_path(repo: Path) -> None:
    _commit(repo, "old.py", "content\n")
    _git(repo, "mv", "old.py", "new.py")

    assert changed_files(repo) == frozenset({ChangedFile("old.py", ChangeKind.RENAMED)})


def test_rename_with_further_worktree_edit_is_still_renamed_at_old_path(repo: Path) -> None:
    _commit(repo, "old.py", "content\n")
    _git(repo, "mv", "old.py", "new.py")
    (repo / "new.py").write_text("content\nmore\n")

    assert changed_files(repo) == frozenset({ChangedFile("old.py", ChangeKind.RENAMED)})


def test_paths_are_repo_relative_regardless_of_invocation_directory(repo: Path) -> None:
    _commit(repo, "a.py", "one\n")
    subdir_file = repo / "sub" / "b.py"
    subdir_file.parent.mkdir()
    subdir_file.write_text("content\n")
    _git(repo, "add", "sub/b.py")

    assert changed_files(repo) == frozenset({ChangedFile("sub/b.py", ChangeKind.ADDED)})


def test_union_of_staged_unstaged_and_untracked(repo: Path) -> None:
    _commit(repo, "staged.py", "one\n")
    _commit(repo, "unstaged.py", "one\n")
    (repo / "staged.py").write_text("two\n")
    _git(repo, "add", "staged.py")
    (repo / "unstaged.py").write_text("two\n")
    (repo / "untracked.py").write_text("new\n")

    assert changed_files(repo) == frozenset(
        {
            ChangedFile("staged.py", ChangeKind.MODIFIED),
            ChangedFile("unstaged.py", ChangeKind.MODIFIED),
            ChangedFile("untracked.py", ChangeKind.ADDED),
        }
    )


def test_no_changes_returns_empty_set(repo: Path) -> None:
    _commit(repo, "a.py", "one\n")

    assert changed_files(repo) == frozenset()


def test_outside_a_git_repository_raises(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    with pytest.raises(GitStatusError):
        changed_files(not_a_repo)


def test_git_not_on_path_raises(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _commit(repo, "a.py", "one\n")
    monkeypatch.setattr("smoke_optimiser.downwind.changes.shutil.which", lambda _name: None)

    with pytest.raises(GitStatusError):
        changed_files(repo)


def test_merge_conflict_is_modified(repo: Path) -> None:
    _commit(repo, "f.py", "base\n")
    _git(repo, "checkout", "-q", "-b", "other")
    (repo / "f.py").write_text("other\n")
    _git(repo, "commit", "-q", "-a", "-m", "other")
    _git(repo, "checkout", "-q", "-")
    (repo / "f.py").write_text("mainline\n")
    _git(repo, "commit", "-q", "-a", "-m", "mainline")
    subprocess.run(["git", "merge", "other"], cwd=repo, check=False, capture_output=True, text=True)  # noqa: S607

    assert changed_files(repo) == frozenset({ChangedFile("f.py", ChangeKind.MODIFIED)})
