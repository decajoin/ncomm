"""Safety guardrails for ncomm's mutating git operations.

ncomm only ever runs three things on the user's behalf: `git add <explicit
paths>`, `git commit -m <msg>`, and (when asked) `git commit --amend -m <msg>`.
It NEVER runs push, force-push, reset, rebase, or cherry-pick — those are out
of scope and deliberately unsupported. This module documents that contract and
gates the one mildly dangerous operation (--amend rewrites history) behind a
typed confirmation.
"""

from __future__ import annotations

# Operations ncomm refuses to perform or assist with. Listed so the help text
# and any future "do X" request can point at a single source of truth.
OUT_OF_SCOPE = [
    "git push / git push --force  (ncomm never pushes)",
    "git reset --hard / reset --soft  (rewrites the index/HEAD)",
    "git rebase  (rewrites history)",
    "git cherry-pick / revert  (use git directly)",
    "git commit --no-verify  (ncomm never bypasses hooks)",
]


def is_out_of_scope(op: str) -> bool:
    """True if `op` is something ncomm must never run."""
    op = op.strip().lower()
    markers = ("push", "reset --hard", "reset --soft", "rebase", "cherry-pick",
               "revert ", "--no-verify", "--force")
    return any(m in op for m in markers)


def amend_requires_typed_yes(typed: str) -> bool:
    """`--amend` rewrites the previous commit; require the full word 'yes'."""
    return typed.strip() == "yes"
