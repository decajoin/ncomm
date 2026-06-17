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

 Commit this? [y/n/e/q] (y): y
✓ a1b2c3d  feat(auth): add OAuth2 login flow
```

## Why

A real working tree is rarely one logical change — it's a feature, a bugfix, and
a dependency bump tangled together. Stuffing them into one commit makes
`git bisect`, revert, and review harder. `ncomm` groups them for you, then
commits each group with only its explicit file list.

## Install

```bash
uv tool install ncomm-cli
# or
pip install ncomm-cli
```

From source:

```bash
git clone https://github.com/decajoin/ncomm
cd ncomm
uv sync
uv run ncomm --version
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
```

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
