"""ncomm's safety contract — the single source of truth for what it refuses.

ncomm only ever runs two things on the user's behalf: `git add <explicit
paths>` and `git commit -m <msg> -- <paths>`. It NEVER pushes, force-pushes,
resets, rebases, cherry-picks, or bypasses hooks — those are out of scope and
deliberately unsupported. `OUT_OF_SCOPE` documents that boundary and is shown
verbatim to the user by `ncomm config show`, so the promise the user reads is
the same list the code is built around.

(History-rewriting `--amend` is planned; when it lands it will carry its own
typed-confirmation gate next to its call site rather than as a free-floating
predicate here.)
"""

from __future__ import annotations

# Operations ncomm refuses to perform or assist with. Rendered by `config show`.
OUT_OF_SCOPE = [
    "git push / git push --force  (ncomm never pushes)",
    "git reset --hard / reset --soft  (rewrites the index/HEAD)",
    "git rebase  (rewrites history)",
    "git cherry-pick / revert  (use git directly)",
    "git commit --no-verify  (ncomm never bypasses hooks)",
]
