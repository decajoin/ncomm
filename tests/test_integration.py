"""Integration tests for ncomm.gitops against a real temporary git repo.

These exercise the parts that can't be covered by pure-function tests: porcelain
parsing and the staging/commit primitives. They guard the property that
committing one group never sweeps in content the user had pre-staged.
"""

from __future__ import annotations

import subprocess

import pytest

from ncomm.gitops import collect_changes, commit, stage


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


def test_deletion_stages_and_commits(repo):
    (repo / "a.txt").unlink()

    changes = collect_changes()
    assert any(fc.path == "a.txt" and fc.status == "D" for fc in changes.files)

    stage(["a.txt"], cwd=changes.root)
    commit("chore: drop a.txt", cwd=changes.root, paths=["a.txt"])
    assert _committed_files(repo) == {"a.txt"}
    assert not (repo / "a.txt").exists()
