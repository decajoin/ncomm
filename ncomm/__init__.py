"""ncomm — AI-assisted Conventional Commits.

Splits a working tree into one or more semantic commits (Conventional Commits
format), lets you review each group, then commits them. The natural sibling of
nlsh: nlsh proposes a command, ncomm proposes your commits.
"""

from __future__ import annotations

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("ncomm")
except Exception:  # pragma: no cover - not installed (e.g. running from source)
    __version__ = "0.2.0"

__all__ = ["__version__"]
