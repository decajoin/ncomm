# ncomm

> One command, well-formed commits. The natural sibling of [nlsh](https://github.com/decajoin/nlsh): nlsh proposes a command, **ncomm proposes your commits**.

`ncomm` reads your working tree, asks an LLM (DeepSeek) to split the changes into one or more **Conventional Commits**, shows you each proposed commit, and commits the ones you approve.

```
$ ncomm
 working tree on feature/x
 st  file                       ±
 M   src/auth/oauth.py          +142 -4
 A   src/auth/__init__.py       +3
 ??  tests/test_oauth.py        new
 M   pyproject.toml             +1 -1

Proposed 2 commit(s).

 commit 1/2  feat(auth): add OAuth2 login flow
 Wires the OAuth2 device-code grant into the login view.
 files: src/auth/oauth.py, src/auth/__init__.py, tests/test_oauth.py

 Commit this? (y)es (n)o (e)dit (d)iff (r)egroup (q)uit (y): y
✓ a1b2c3d  feat(auth): add OAuth2 login flow
```

At each commit you can press **y** to accept, **n** to skip, **e** to edit the
message in `$EDITOR`, **d** to show the actual diff for that group, **r** to ask
the model to regroup (optionally with a one-line hint like "split tests out"),
or **q** to stop.

## Why

A real working tree is rarely one logical change — it's a feature, a bugfix, and
a dependency bump tangled together. Stuffing them into one commit makes
`git bisect`, revert, and review harder. `ncomm` groups them for you, then
commits each group with only its explicit file list.

## Install

`ncomm` ships a `uv.lock` and a pinned `requirements.txt`, so you can install
with either **uv** or **pip** — whichever your environment prefers.

### Option A — uv (recommended)

```bash
git clone https://github.com/decajoin/ncomm
cd ncomm
uv sync                # creates .venv, installs pinned runtime deps from uv.lock
uv run ncomm --version
```

Or install it as a global tool:

```bash
uv tool install ncomm
```

### Option B — pip

Runtime dependencies are pinned in `requirements.txt` (runtime only — no
pytest/ruff), so a plain `pip` install is reproducible without uv:

```bash
git clone https://github.com/decajoin/ncomm
cd ncomm
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt                      # pinned runtime deps
pip install -e .                                     # install ncomm itself (editable)
ncomm --version
```

Or from PyPI:

```bash
pip install ncomm
```

### Regenerating the lockfile / requirements

Both files are kept in sync. If you change `pyproject.toml`:

```bash
uv lock                                   # refresh uv.lock
uv export --format requirements-txt --no-hashes --no-emit-project --no-dev \
    -o requirements.txt                   # refresh runtime-only requirements.txt
```

## First-time setup

```bash
ncomm config set-key        # paste your DeepSeek API key (stored mode 0600)
ncomm config show
```

Or via env: `export DEEPSEEK_API_KEY=sk-...`

## Usage

```
ncomm                  # group, review each commit, commit approved ones
ncomm -y               # commit all proposed groups without prompting
ncomm -n               # dry run: show proposed commits, commit nothing
ncomm --no-group       # force a single commit covering all changes
ncomm --pro            # use the stronger model for this run
ncomm -m <model>       # override the model id
ncomm --lang en        # messages in English (default: en; use zh for Chinese)

# Path filtering (fnmatch globs, repeatable) — leave WIP out of the commit:
ncomm --only 'src/auth/**' --only 'tests/test_auth*'   # only this slice
ncomm --exclude '*.lock' --exclude 'tmp/*'             # everything but these
```

`--only` / `--exclude` filter the changed set *before* grouping, so excluded
files stay untouched in your working tree and never need to be committed.

## Safety contract

`ncomm` only ever runs three things on your behalf:

- `git add <explicit paths>` — never `git add -A` / `git add .`
- `git commit -m <message>` — hooks are never bypassed
- *(future)* `git commit --amend` — gated behind a typed `yes`

It **never** pushes, force-pushes, resets, or rebases. After committing, review
with `git log` and push when you're ready.

If the model's file assignment doesn't cover every changed file exactly, ncomm
aborts rather than commit a wrong grouping — re-run, or use `--no-group`.

## How it works

1. `git diff HEAD` + untracked file contents are gathered into one diff bundle,
   with per-file patches truncated (head + tail) so a generated/lockfile doesn't
   blow the token budget.
2. The bundle goes to DeepSeek with a Conventional Commits system prompt; the
   model returns `{"groups": [...]}`.
3. ncomm validates that the union of group files == the set of changed files.
4. For each approved group: `git add <its files>` then `git commit -m <msg>`.

## Configuration

`~/.config/ncomm/config.toml` (or `$NCOMM_CONFIG`):

```toml
api_key = "sk-..."
base_url = "https://api.deepseek.com"
model = "deepseek-v4-flash"
```

Env overrides: `DEEPSEEK_API_KEY` / `NCOMM_API_KEY`, `DEEPSEEK_BASE_URL`,
`NCOMM_MODEL`, `NCOMM_LANG`.

## License

MIT
