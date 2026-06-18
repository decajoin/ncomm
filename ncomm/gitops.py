"""Git operations for ncomm.

Only two mutating operations ever run through this module:
  - `stage(paths)`  — `git add` of *explicitly listed* paths (never `git add -A`)
  - `commit(message, paths=…)` — `git commit -m … -- <paths>`. The trailing
    pathspec is load-bearing: it commits ONLY those paths, so unrelated content
    the user had already staged never gets swept into the wrong commit.

Everything else is read-only (diff / status / ls-files). The combined diff is
`git diff HEAD`, which captures every uncommitted change to tracked files in one
shot; untracked files are listed separately and their content read directly.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

# Per-file patch budget. A file whose diff exceeds this is shown head + tail
# with the middle elided, so a giant generated/lockfile doesn't blow the token
# budget while still signalling "this file changed a lot".
PATCH_HEAD = 60
PATCH_TAIL = 30
UNTRACKED_CONTENT_LINES = 80


class GitError(RuntimeError):
    pass


def _run(args: List[str], *, cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            # core.quotepath=false keeps non-ASCII paths (e.g. CJK filenames)
            # literal in diff/status output instead of octal-escaped + quoted,
            # so _split_patches and the numstat parse can match them by path.
            ["git", "-c", "core.quotepath=false", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=check,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or "").strip()
        raise GitError(f"git {' '.join(args)} failed: {msg[:300]}") from exc


def repo_root() -> str:
    """Return the repo root, or raise GitError if not inside a repo."""
    out = _run(["rev-parse", "--show-toplevel"], cwd=".", check=True)
    root = out.stdout.strip()
    if not root:
        raise GitError("Not inside a git repository.")
    return root


def current_branch(cwd: str) -> str:
    out = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, check=False)
    name = out.stdout.strip()
    return name or "DETACHED"


@dataclass
class FileChange:
    path: str
    status: str            # porcelain XY code, simplified to a single token
    added: int = 0
    deleted: int = 0


@dataclass
class Changes:
    """Everything ncomm shows to the model and to the user."""

    branch: str
    root: str = ""
    files: List[FileChange] = field(default_factory=list)
    diff_bundle: str = ""          # truncated combined patch + untracked content
    truncated_files: List[str] = field(default_factory=list)
    # new_path -> old_path for staged renames. Porcelain reports a rename as a
    # single file (the new path), but committing it must also carry the old
    # path's deletion or the rename is left half-applied. Looked up at commit.
    renames: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.files


def _split_patches(diff_text: str) -> List[Tuple[str, str]]:
    """Split a combined `git diff` into (filepath, patch) pairs."""
    chunks: List[Tuple[str, str]] = []
    current_path = None
    current_lines: List[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current_path is not None:
                chunks.append((current_path, "\n".join(current_lines)))
            # "diff --git a/foo b/foo"  ->  take the b/ path
            tail = line.split(" b/", 1)
            current_path = tail[1] if len(tail) == 2 else line.split()[-1]
            current_lines = [line]
        else:
            if current_path is None:
                # preamble before any diff header (shouldn't happen for HEAD diff)
                continue
            current_lines.append(line)
    if current_path is not None:
        chunks.append((current_path, "\n".join(current_lines)))
    return chunks


def _truncate_patch(path: str, patch: str) -> Tuple[str, bool]:
    lines = patch.splitlines()
    if len(lines) <= PATCH_HEAD + PATCH_TAIL:
        return patch, False
    head = lines[:PATCH_HEAD]
    tail = lines[-PATCH_TAIL:]
    omitted = len(lines) - PATCH_HEAD - PATCH_TAIL
    elided = "\n".join(head + [f"@@ ... {omitted} lines truncated ..."] + tail)
    return elided, True


def _simplify_status(xy: str) -> str:
    """Map porcelain XY into a single readable token."""
    if not xy:
        return "M"
    if xy == "??":
        return "?"
    if "R" in xy:
        return "R"
    if "C" in xy:
        return "C"
    if "A" in xy:
        return "A"
    if "D" in xy:
        return "D"
    return "M"


def _parse_porcelain(cwd: str) -> Tuple[List[FileChange], List[str], dict[str, str]]:
    # -uall expands untracked directories into individual files; otherwise git
    # collapses `tests/` and we'd try to read a directory as a file.
    out = _run(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=cwd, check=True,
    )
    files: List[FileChange] = []
    untracked: List[str] = []
    renames: dict[str, str] = {}
    # -z separates records by NUL. For a rename/copy the record is the NEW path,
    # immediately followed by a second NUL record holding the ORIGINAL path
    # (i.e. `R<NUL>new<NUL>old`). Verified against `git status --porcelain -z`.
    records = out.stdout.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        if not rec:
            i += 1
            continue
        xy = rec[:2]
        path = rec[3:]
        old_path = None
        if "R" in xy or "C" in xy:
            old_path = records[i + 1] if i + 1 < len(records) else None
            i += 2
        else:
            i += 1
        if not path:
            continue
        token = _simplify_status(xy)
        files.append(FileChange(path=path, status=token))
        if token == "?":
            untracked.append(path)
        # A rename removes the old path; a copy leaves it in place. Only pair the
        # deletion for renames so committing the new path carries it along.
        if token == "R" and old_path:
            renames[path] = old_path
    return files, untracked, renames


def _read_untracked_content(root: str, path: str) -> str:
    """Read an untracked file's content, capped to UNTRACKED_CONTENT_LINES."""
    full = Path(root) / path
    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(could not read: {exc})"
    lines = text.splitlines()
    if len(lines) <= UNTRACKED_CONTENT_LINES:
        return text
    head = lines[:UNTRACKED_CONTENT_LINES]
    omitted = len(lines) - UNTRACKED_CONTENT_LINES
    return "\n".join(head + [f"... {omitted} more lines truncated ..."])


def collect_changes() -> Changes:
    """Gather every uncommitted change relative to HEAD into one Changes object."""
    root = repo_root()
    branch = current_branch(root)
    files, untracked, renames = _parse_porcelain(root)

    # Per-file stat (additions/deletions) for the tracked portion.
    stat_out = _run(["diff", "HEAD", "--numstat"], cwd=root, check=False)
    add_map: dict[str, Tuple[int, int]] = {}
    for line in stat_out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            added, deleted, path = parts
            try:
                add_map[path] = (int(added) if added != "-" else 0,
                                 int(deleted) if deleted != "-" else 0)
            except ValueError:
                continue

    for fc in files:
        if fc.status == "?":
            continue
        a, d = add_map.get(fc.path, (0, 0))
        fc.added, fc.deleted = a, d

    # Build the diff bundle: truncated per-file patches + untracked content.
    diff_out = _run(["diff", "HEAD"], cwd=root, check=False)
    patches = _split_patches(diff_out.stdout)

    bundle_parts: List[str] = [f"Branch: {branch}", ""]
    truncated: List[str] = []

    summary_lines = []
    for fc in files:
        if fc.status == "?":
            summary_lines.append(f"?? {fc.path}  (untracked)")
        else:
            summary_lines.append(f"{fc.status} {fc.path}  (+{fc.added} -{fc.deleted})")
    if summary_lines:
        bundle_parts.append("Changed files:")
        bundle_parts.extend(f"  {s}" for s in summary_lines)
        bundle_parts.append("")

    patch_map = {p: patch for p, patch in patches}
    for fc in files:
        if fc.status == "?":
            bundle_parts.append(f"--- new file: {fc.path} ---")
            bundle_parts.append(_read_untracked_content(root, fc.path))
            bundle_parts.append("")
        else:
            patch = patch_map.get(fc.path, "(no patch)")
            tpatch, did_trunc = _truncate_patch(fc.path, patch)
            if did_trunc:
                truncated.append(fc.path)
            bundle_parts.append(patch if not did_trunc else tpatch)
            bundle_parts.append("")

    return Changes(
        branch=branch,
        root=root,
        files=files,
        diff_bundle="\n".join(bundle_parts).strip(),
        truncated_files=truncated,
        renames=renames,
    )


def stage(paths: List[str], cwd: str) -> None:
    """Stage the given explicit paths. Never `git add -A` / `git add .`."""
    if not paths:
        return
    _run(["add", "--", *paths], cwd=cwd, check=True)


def diff_for_paths(paths: List[str], *, root: str, untracked: "List[str] | None" = None) -> str:
    """Return a printable diff for the given paths (for on-demand `d` review).

    Tracked paths are shown via `git diff HEAD`; untracked paths have no diff to
    show, so their (capped) content is appended under a `new file` header.
    """
    untracked_set = set(untracked or ())
    tracked = [p for p in paths if p not in untracked_set]
    parts: List[str] = []
    if tracked:
        out = _run(["diff", "HEAD", "--", *tracked], cwd=root, check=False)
        if out.stdout.strip():
            parts.append(out.stdout.rstrip("\n"))
    for p in untracked or ():
        parts.append(f"--- new file: {p} ---")
        parts.append(_read_untracked_content(root, p))
    return "\n".join(parts)


def commit(message: str, cwd: str, *, paths: "List[str] | None" = None) -> str:
    """Create a commit. Returns the new HEAD short sha.

    When `paths` is given the commit is scoped to that pathspec (`git commit
    -- <paths>`), so only those paths are committed even if the user had other
    content staged in the index. Without it, the whole index is committed.
    """
    args = ["commit", "-m", message]
    if paths:
        args += ["--", *paths]
    out = _run(args, cwd=cwd, check=True)
    sha = _run(["rev-parse", "--short", "HEAD"], cwd=cwd, check=True)
    return (sha.stdout.strip() or out.stdout.strip())


def ensure_clean_since(snapshot_paths: set[str], cwd: str) -> List[str]:
    """Detect files that changed since we analysed the tree.

    Returns paths that are dirty now but weren't part of the snapshot we showed
    the model and the user — e.g. an IDE auto-format that landed during review.
    The pathspec-scoped commit already keeps these out of any commit; surfacing
    them just tells the user their commits won't match a stale review.
    Renames/copies (R/C) are skipped because their record carries two paths.
    """
    out = _run(["status", "--porcelain=v1"], cwd=cwd, check=False)
    surprises: List[str] = []
    for line in out.stdout.splitlines():
        if not line:
            continue
        path = line[3:]
        if "R" in line[:2] or "C" in line[:2]:
            continue
        if path not in snapshot_paths:
            surprises.append(path)
    return surprises
