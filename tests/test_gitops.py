"""Tests for ncomm.gitops — diff parsing & patch truncation (no live git)."""

from ncomm.gitops import FileChange, _simplify_status, _split_patches, _truncate_patch


def test_split_patches_extracts_paths():
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "+line1\n"
        "diff --git a/bar.py b/bar.py\n"
        "-line2\n"
    )
    chunks = _split_patches(diff)
    assert [p for p, _ in chunks] == ["foo.py", "bar.py"]


def test_split_patches_handles_renames_uses_b_path():
    diff = "diff --git a/old.py b/new.py\n+line\n"
    chunks = _split_patches(diff)
    assert chunks[0][0] == "new.py"


def test_truncate_short_patch_unchanged():
    patch = "\n".join(f"line {i}" for i in range(10))
    out, truncated = _truncate_patch("x.py", patch)
    assert truncated is False
    assert out == patch


def test_truncate_long_patch_elides_middle():
    patch = "\n".join(f"line {i}" for i in range(200))
    out, truncated = _truncate_patch("x.py", patch)
    assert truncated is True
    assert "truncated" in out
    assert out.count("\n") < 200


def test_simplify_status_tokens():
    assert _simplify_status("??") == "?"
    assert _simplify_status("M ") == "M"
    assert _simplify_status("A ") == "A"
    assert _simplify_status("D ") == "D"
    assert _simplify_status("R ") == "R"
    assert _simplify_status("MM") == "M"


def test_file_change_defaults():
    fc = FileChange(path="a.py", status="M")
    assert fc.added == 0 and fc.deleted == 0
