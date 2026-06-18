"""Integration tests for ncomm.gitops against a real temporary git repo.

These exercise the parts that can't be covered by pure-function tests: porcelain
parsing (including renames) and the staging/commit primitives. They guard two
properties the unit tests can't:

  1. committing one group never sweeps in content the user had pre-staged, and
  2. a renamed file's old-path deletion travels with its new path.
"""

from __future__ import annotations

import subprocess

import pytest

from ncomm.gitops import collect_changes, commit, diff_for_paths, stage


def _git(repo, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    (tmp_path / "keep.txt").write_text("keep\n")
    (tmp_path / "unrel.txt").write_text("u\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    # collect_changes / stage / commit run git in the current directory.
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _committed_files(repo, ref: str = "HEAD") -> set[str]:
    out = _git(repo, "show", "--name-only", "--pretty=format:", ref)
    return {line for line in out.splitlines() if line}


def test_prestaged_content_does_not_leak_into_commit(repo):
    # User pre-stages an unrelated file, then modifies another file (unstaged).
    (repo / "unrel.txt").write_text("u-CHANGED\n")
    _git(repo, "add", "unrel.txt")
    (repo / "keep.txt").write_text("keep\nmore\n")

    changes = collect_changes()
    paths = {fc.path for fc in changes.files}
    assert paths == {"unrel.txt", "keep.txt"}

    # Commit ONLY keep.txt — the realistic "group 1 of N" case.
    stage(["keep.txt"], cwd=changes.root)
    commit("test: keep only", cwd=changes.root, paths=["keep.txt"])

    assert _committed_files(repo) == {"keep.txt"}
    # unrel.txt must still be staged and uncommitted, exactly as the user left it.
    status = _git(repo, "status", "--porcelain=v1")
    assert "M  unrel.txt" in status


def test_rename_carries_old_path_deletion(repo):
    # A staged rename is reported by porcelain as the single new path.
    _git(repo, "mv", "a.txt", "b.txt")

    changes = collect_changes()
    paths = {fc.path for fc in changes.files}
    assert "b.txt" in paths
    assert "a.txt" not in paths  # porcelain reports only the new path
    assert changes.renames == {"b.txt": "a.txt"}

    # Commit the new path; the old path's deletion must travel with it.
    stage(["b.txt"], cwd=changes.root)
    rename_olds = [changes.renames[p] for p in ["b.txt"] if p in changes.renames]
    commit("refactor: rename a to b", cwd=changes.root, paths=["b.txt"] + rename_olds)

    # The new tree has b.txt and no longer has a.txt, and — the property the fix
    # guarantees — the working tree is clean: the old path's deletion was carried
    # into the commit rather than left dangling as an uncommitted ` D a.txt`.
    tracked = set(_git(repo, "ls-files").splitlines())
    assert "b.txt" in tracked and "a.txt" not in tracked
    status = _git(repo, "status", "--porcelain=v1")
    assert status.strip() == ""  # working tree clean — rename fully applied


def test_deletion_stages_and_commits(repo):
    (repo / "a.txt").unlink()

    changes = collect_changes()
    assert any(fc.path == "a.txt" and fc.status == "D" for fc in changes.files)

    stage(["a.txt"], cwd=changes.root)
    commit("chore: drop a.txt", cwd=changes.root, paths=["a.txt"])
    assert _committed_files(repo) == {"a.txt"}
    assert not (repo / "a.txt").exists()


def test_diff_for_paths_tracked_and_untracked(repo):
    (repo / "keep.txt").write_text("keep\nADDED\n")
    (repo / "fresh.txt").write_text("brand new line\n")

    changes = collect_changes()
    text = diff_for_paths(["keep.txt", "fresh.txt"], root=changes.root, untracked=["fresh.txt"])

    # Tracked file shows up as a real unified diff; untracked file shows content.
    assert "diff --git a/keep.txt b/keep.txt" in text
    assert "+ADDED" in text
    assert "new file: fresh.txt" in text
    assert "brand new line" in text
