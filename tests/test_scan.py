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


def test_detects_underscore_style_secret_identifiers():
    # The most common real-world naming: SCREAMING_SNAKE with the keyword inside.
    f = scan.scan_patch("c.py", _patch(
        'AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCY"',
        'DATABASE_PASSWORD = "hunter2-s3cr3t"',
    ))
    assert len(f) == 2
    assert all(x.kind == "secret" for x in f)


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


def test_entropy_flags_unstructured_token_as_low_confidence():
    # A random-looking token assigned to an innocuous name — no named rule hits.
    f = scan.scan_patch("f.py", _patch("blob = 'Zx9Qm2Lp7Vt4Rk8Nf1Wc6Hb3Yd5Sg0Aj'"))
    assert len(f) == 1
    assert f[0].kind == "secret" and f[0].confidence == "low"
    assert f[0].rule == "high-entropy string"


def test_entropy_ignores_git_hash_and_english():
    # A sha1 hash and a normal sentence are not high-entropy secrets.
    f = scan.scan_patch("f.py", _patch(
        "rev = 'da39a3ee5e6b4b0d3255bfef95601890afd80709'",
        "msg = 'the quick brown fox jumps over the lazy dog again'",
    ))
    assert f == []


def test_entropy_skips_lockfiles():
    token = "Zx9Qm2Lp7Vt4Rk8Nf1Wc6Hb3Yd5Sg0Aj"
    assert scan.scan_patch("uv.lock", _patch(f"hash = '{token}'")) == []
    assert scan.scan_patch("app.py", _patch(f"hash = '{token}'"))  # but flagged elsewhere


def test_named_rule_beats_entropy_no_double_report():
    f = scan.scan_patch("f.py", _patch("KEY = 'AKIAIOSFODNN7EXAMPLE'"))
    assert len(f) == 1 and f[0].confidence == "high"


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
