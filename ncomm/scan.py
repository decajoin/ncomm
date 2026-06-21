"""Local, dependency-free scanning of a diff before it becomes a commit.

Two jobs, both run on the *added* lines only (never on context or removed
lines), so ncomm flags what you're about to introduce, not what's already there:

  - secrets: credentials / keys that shouldn't be committed (high severity)
  - debug leftovers: stray breakpoints / console logging (low severity)

Nothing here calls out to the network or the model; it's pure regex over text.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
# (label, compiled pattern). Kept deliberately specific to limit false alarms.
SECRET_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("provider api key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    (
        "hardcoded secret assignment",
        re.compile(
            # The keyword may be embedded in an identifier (AWS_SECRET_ACCESS_KEY,
            # DATABASE_PASSWORD, apiKey), so don't anchor it with \b on both sides.
            r"""(?ix)\b[a-z0-9_]*"""
            r"""(?:api[_-]?key|secret|token|password|passwd|access[_-]?key|credential)"""
            r"""[a-z0-9_]*\s*[:=]\s*['"]([^'"\s]{6,})['"]"""
        ),
    ),
]

DEBUG_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("pdb breakpoint", re.compile(r"\b(?:pdb\.set_trace|breakpoint)\s*\(")),
    ("import pdb", re.compile(r"\bimport\s+i?pdb\b")),
    ("console logging", re.compile(r"\bconsole\.(?:log|debug|trace)\s*\(")),
    ("debugger statement", re.compile(r"(?:^|\s)debugger\s*;?\s*$")),
    ("ruby binding.pry", re.compile(r"\bbinding\.pry\b")),
    ("rust dbg!", re.compile(r"\bdbg!\s*\(")),
]

# Values that look like obvious placeholders rather than real secrets.
_PLACEHOLDER = re.compile(
    r"(?i)(x{3,}|your[_-]?|example|placeholder|changeme|dummy|<[^>]+>|\$\{|os\.environ|getenv)"
)

# --------------------------------------------------------------------------- #
# Entropy detection — catches high-randomness tokens the named rules miss
# (custom/unstructured secrets), as a *low-confidence* signal.
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_\-]{20,}")
_HEX_HASH = re.compile(r"\A[0-9a-f]{40}\Z|\A[0-9a-f]{64}\Z")   # sha1 / sha256: usually not secrets
_ENTROPY_MIN_LEN = 24
_ENTROPY_THRESHOLD = 4.0          # bits/char; random base64 sits ~5.5–6, words ~3
# Files where high-entropy tokens are expected (hashes, checksums) — skip them.
_LOCKISH = ("lock", ".sum")


def _shannon(s: str) -> float:
    n = len(s)
    if n <= 1:
        return 0.0
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def _is_lockish(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    return any(hint in base for hint in _LOCKISH)


def _entropy_findings(path: str, line_no: int, text: str) -> List["Finding"]:
    if _is_lockish(path):
        return []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if len(tok) < _ENTROPY_MIN_LEN or _HEX_HASH.match(tok) or _PLACEHOLDER.search(tok):
            continue
        if _shannon(tok) >= _ENTROPY_THRESHOLD:
            snippet = text.replace(tok, _mask(tok)).strip()[:160]
            return [Finding(path, line_no, "secret", "high-entropy string", snippet, "low")]
    return []


@dataclass
class Finding:
    path: str
    line_no: int
    kind: str          # "secret" | "debug"
    rule: str
    snippet: str
    confidence: str = "high"   # "high" = structural match (blocks); "low" = entropy


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "…"
    return f"{value[:4]}…{value[-4:]}"


def _scan_line(path: str, line_no: int, text: str) -> List[Finding]:
    found: List[Finding] = []
    for label, pat in SECRET_RULES:
        m = pat.search(text)
        if not m:
            continue
        secret = m.group(m.lastindex) if m.lastindex else m.group(0)
        if label == "hardcoded secret assignment" and _PLACEHOLDER.search(secret):
            continue
        masked = text.replace(secret, _mask(secret))
        found.append(Finding(path, line_no, "secret", label, masked.strip()[:160]))
        break  # one secret hit per line is enough
    # Entropy is a fallback for the unstructured tokens the named rules miss.
    if not any(f.kind == "secret" for f in found):
        found.extend(_entropy_findings(path, line_no, text))
    for label, pat in DEBUG_RULES:
        if pat.search(text):
            found.append(Finding(path, line_no, "debug", label, text.strip()[:160]))
            break
    return found


def _added_lines_from_patch(patch: str) -> Iterable[Tuple[int, str]]:
    """Yield (new-file line number, text) for each '+' line in a unified diff."""
    new_no = None
    for line in patch.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_no = int(m.group(1)) if m else 1
            continue
        if new_no is None or line.startswith("\\"):
            continue
        if line.startswith("+"):
            yield new_no, line[1:]
            new_no += 1
        elif not line.startswith("-"):
            new_no += 1


def scan_patch(path: str, patch: str) -> List[Finding]:
    out: List[Finding] = []
    for line_no, text in _added_lines_from_patch(patch):
        out.extend(_scan_line(path, line_no, text))
    return out


def scan_new_file(path: str, content: str) -> List[Finding]:
    out: List[Finding] = []
    for i, text in enumerate(content.splitlines(), 1):
        out.extend(_scan_line(path, i, text))
    return out


# --------------------------------------------------------------------------- #
# .gitignore suggestions for untracked junk
# --------------------------------------------------------------------------- #
# Directory names that should almost always be ignored, mapped to their pattern.
_IGNORE_DIRS = {
    "__pycache__": "__pycache__/",
    "node_modules": "node_modules/",
    ".venv": ".venv/",
    "venv": "venv/",
    "dist": "dist/",
    "build": "build/",
    ".pytest_cache": ".pytest_cache/",
    ".ruff_cache": ".ruff_cache/",
    ".mypy_cache": ".mypy_cache/",
    ".tox": ".tox/",
    "htmlcov": "htmlcov/",
    ".idea": ".idea/",
    ".vscode": ".vscode/",
}
# Exact basenames.
_IGNORE_NAMES = {
    ".DS_Store": ".DS_Store",
    "Thumbs.db": "Thumbs.db",
    ".coverage": ".coverage",
}
# Suffix -> glob pattern.
_IGNORE_SUFFIXES = {
    ".pyc": "*.pyc",
    ".pyo": "*.pyo",
    ".log": "*.log",
    ".so": "*.so",
    ".o": "*.o",
    ".class": "*.class",
}


def _ignore_pattern_for(path: str) -> "str | None":
    parts = path.split("/")
    for part in parts:
        if part in _IGNORE_DIRS:
            return _IGNORE_DIRS[part]
        if part.endswith(".egg-info"):
            return "*.egg-info/"
    base = parts[-1]
    if base in _IGNORE_NAMES:
        return _IGNORE_NAMES[base]
    if base == ".env" or base.startswith(".env."):
        return ".env"
    for suffix, pattern in _IGNORE_SUFFIXES.items():
        if base.endswith(suffix):
            return pattern
    return None


def gitignore_candidates(paths: Iterable[str]) -> Dict[str, List[str]]:
    """Map suggested .gitignore pattern -> the untracked paths it would cover."""
    out: Dict[str, List[str]] = {}
    for p in paths:
        pattern = _ignore_pattern_for(p)
        if pattern:
            out.setdefault(pattern, []).append(p)
    return out


def append_gitignore(root: str, patterns: Iterable[str]) -> List[str]:
    """Append patterns not already present to <root>/.gitignore. Returns the
    patterns actually added (in input order, de-duplicated)."""
    path = Path(root) / ".gitignore"
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    present = {line.strip() for line in existing_lines}
    to_add: List[str] = []
    for pat in patterns:
        if pat not in present and pat not in to_add:
            to_add.append(pat)
    if not to_add:
        return []
    lines = list(existing_lines)
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("# Added by ncomm")
    lines.extend(to_add)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return to_add
