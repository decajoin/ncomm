"""Tests for ncomm.scan — secret / debug-leftover detection on added lines."""

from ncomm import scan


def _patch(*added: str) -> str:
    body = "\n".join("+" + a for a in added)
    return "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -0,0 +1,9 @@\n" + body


def test_detects_aws_key():
    f = scan.scan_patch("f.py", _patch("KEY = 'AKIAIOSFODNN7EXAMPLE'"))
    assert any(x.kind == "secret" and "AWS" in x.rule for x in f)


def test_detects_private_key_and_provider_key():
    f = scan.scan_patch("f.py", _patch("-----BEGIN RSA PRIVATE KEY-----", "tok = 'sk-' + 'a'*30"))
    kinds = {x.rule for x in f}
    assert any("private key" in k for k in kinds)


def test_detects_debug_leftovers():
    f = scan.scan_patch("f.py", _patch("breakpoint()", "console.log('x')", "import pdb"))
    rules = {x.rule for x in f if x.kind == "debug"}
    assert "pdb breakpoint" in rules
    assert "console logging" in rules


def test_ignores_placeholders():
    f = scan.scan_patch("f.py", _patch("password = 'your_password_here'", "api_key = '${API_KEY}'"))
    assert f == []


def test_only_scans_added_lines():
    # A removed secret (leading '-') must not be flagged.
    patch = (
        "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n"
        "-KEY = 'AKIAIOSFODNN7EXAMPLE'\n+KEY = load_key()\n"
    )
    assert scan.scan_patch("f.py", patch) == []


def test_new_file_scan_reports_line_numbers():
    findings = scan.scan_new_file("c.py", "ok = 1\nbreakpoint()\n")
    assert findings and findings[0].line_no == 2


def test_secret_snippet_is_masked():
    f = scan.scan_patch("f.py", _patch("token = 'sk-abcdefghij1234567890XYZ'"))
    assert f and "sk-abcdefghij1234567890XYZ" not in f[0].snippet


def test_gitignore_candidates_maps_patterns():
    cand = scan.gitignore_candidates([
        "src/__pycache__/m.cpython-311.pyc",
        "dist/pkg.whl",
        ".DS_Store",
        "logs/app.log",
        ".env",
        "ncomm.egg-info/PKG-INFO",
        "src/real_code.py",        # not junk -> no pattern
    ])
    assert "__pycache__/" in cand
    assert "dist/" in cand
    assert ".DS_Store" in cand
    assert "*.log" in cand
    assert ".env" in cand
    assert "*.egg-info/" in cand
    assert "src/real_code.py" not in str(cand)


def test_append_gitignore_dedups_and_preserves(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("*.log\n")
    added = scan.append_gitignore(str(tmp_path), ["*.log", "dist/", "dist/"])
    assert added == ["dist/"]                     # *.log already present, dist/ once
    text = gi.read_text()
    assert text.startswith("*.log\n")             # original kept
    assert "dist/" in text

    # Writing to a repo with no .gitignore creates it.
    fresh = tmp_path / "sub"
    fresh.mkdir()
    assert scan.append_gitignore(str(fresh), ["__pycache__/"]) == ["__pycache__/"]
    assert (fresh / ".gitignore").exists()
