"""Command-line interface for ncomm."""

from __future__ import annotations

import fnmatch
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import __version__, scan
from .config import (
    PRO_MODEL,
    config_path,
    load_config,
    save_config,
)
from .gitops import (
    Changes,
    GitError,
    collect_changes,
    commit,
    diff_for_paths,
    ensure_clean_since,
    recent_messages,
    stage,
)
from .llm import LLMError, suggest_gitignore, suggest_groups
from .safety import OUT_OF_SCOPE

run_app = typer.Typer(
    add_completion=False,
    help="Turn your working tree into Conventional Commits. ncomm groups the "
    "diff, shows each proposed commit, and commits the ones you approve. Run "
    "`ncomm config set-key` to store your API key.",
)
config_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Manage ncomm configuration (~/.config/ncomm/config.toml).",
)

console = Console()
err_console = Console(stderr=True)

# Cap on how many times one run may re-ask the model to regroup, so a user
# leaning on 'r' can't burn the API indefinitely.
MAX_REGROUP_ROUNDS = 5

# How many recent commit subjects to show the model as a style reference.
STYLE_EXAMPLE_COUNT = 10


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"ncomm {__version__}")
        raise typer.Exit()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _render_changes(changes: Changes) -> None:
    table = Table(title=f"working tree on [bold]{changes.branch}[/bold]", expand=False)
    table.add_column("st", style="dim", width=3)
    table.add_column("file", style="cyan")
    table.add_column("±", style="dim", justify="right")
    for fc in changes.files:
        if fc.status == "?":
            delta = "new"
        else:
            delta = f"+{fc.added} -{fc.deleted}"
        table.add_row(fc.status, fc.path, delta)
    console.print(table)
    if changes.truncated_files:
        console.print(
            f"[dim]note: {len(changes.truncated_files)} file(s) had large diffs "
            f"(head+tail shown to the model).[/dim]"
        )


def _render_group(index: int, total: int, group, edited_message: Optional[str] = None) -> None:
    msg = edited_message if edited_message is not None else group.message
    body = Text()
    body.append(msg, style="bold")
    if group.rationale:
        body.append("\n\n")
        body.append(group.rationale, style="dim italic")
    body.append("\n\nfiles: ", style="dim")
    body.append(", ".join(group.files), style="cyan")
    title = f"commit {index}/{total}  {group.header}"
    console.print(Panel(body, title=title, border_style="green", expand=False))


def _render_group_diff(changes: Changes, group) -> None:
    """Print the actual diff for a group's files (the `d` review option)."""
    status = {fc.path: fc.status for fc in changes.files}
    untracked = [p for p in group.files if status.get(p) == "?"]
    text = diff_for_paths(group.files, root=changes.root, untracked=untracked)
    if not text.strip():
        console.print("[dim](no diff to show)[/dim]")
        return
    console.print(
        Syntax(text, "diff", theme="ansi_dark", word_wrap=False, background_color="default")
    )


def _render_gitignore_candidates(
    candidates: dict, model_set: "frozenset[str]" = frozenset()
) -> None:
    """Print untracked paths that look like they belong in .gitignore."""
    body = Text()
    for pattern, paths in candidates.items():
        body.append(f"  {pattern}", style="bold cyan")
        if pattern in model_set:
            body.append(" (model)", style="dim magenta")
        sample = ", ".join(paths[:3])
        more = f" (+{len(paths) - 3} more)" if len(paths) > 3 else ""
        body.append(f"   ← {sample}{more}\n", style="dim")
    console.print(
        Panel(body, title="these look like they belong in .gitignore",
              border_style="cyan", expand=False)
    )


def _render_findings(findings) -> None:
    """Print the pre-commit scan results.

    High-confidence secrets are red (and gate commits); entropy-based "maybe"
    hits and debug leftovers are yellow advisories.
    """
    secrets = [f for f in findings if f.kind == "secret" and f.confidence == "high"]
    maybe = [f for f in findings if f.kind == "secret" and f.confidence == "low"]
    debug = [f for f in findings if f.kind == "debug"]
    body = Text()
    rows = (
        [("secret", "bold red", "red", f) for f in secrets]
        + [("maybe ", "bold yellow", "yellow", f) for f in maybe]
        + [("debug ", "bold yellow", "yellow", f) for f in debug]
    )
    for tag, tag_style, line_style, f in rows:
        body.append(f"  {tag} ", style=tag_style)
        body.append(f"{f.path}:{f.line_no}  {f.rule}\n", style=line_style)
        body.append(f"      {f.snippet}\n", style="dim")
    title = f"pre-commit scan — {len(secrets)} secret, {len(maybe)} maybe, {len(debug)} debug"
    console.print(
        Panel(body, title=title, border_style="red" if secrets else "yellow", expand=False)
    )


def _validate_groups(groups, changed_paths: set[str]) -> Optional[str]:
    """Return an error string if the model's file assignment is wrong, else None."""
    grouped: set[str] = set()
    for g in groups:
        for f in g.files:
            if f in grouped:
                return f"file assigned to more than one group: {f}"
            grouped.add(f)
    missing = changed_paths - grouped
    extra = grouped - changed_paths
    if extra:
        return f"model referenced unknown files: {sorted(extra)}"
    if missing:
        return f"these changed files were left unassigned: {sorted(missing)}"
    return None


# --------------------------------------------------------------------------- #
# Message editing
# --------------------------------------------------------------------------- #
def _edit_message(message: str) -> str:
    """Open $EDITOR on the message in a temp file; return the edited text.

    Falls back to prompt_toolkit (single line) when no editor is set or stdin
    isn't a tty.
    """
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if editor and sys.stdin.isatty():
        with tempfile.NamedTemporaryFile(
            "w+", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(message)
            tmp_path = fh.name
        # shlex.split so EDITOR values with flags (e.g. "code --wait",
        # "vim -p") are passed as separate argv tokens, not one filename.
        cmd = [*shlex.split(editor), tmp_path]
        try:
            import subprocess

            subprocess.run(cmd, check=True)
            new = Path(tmp_path).read_text(encoding="utf-8")
        except (subprocess.CalledProcessError, OSError) as exc:
            err_console.print(
                f"[yellow]editor command {cmd!r} failed: {exc}; using prompt.[/yellow]"
            )
            new = message
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return new.strip()
    # Fallback: single-line prompt pre-filled.
    try:
        from prompt_toolkit import prompt as ptk_prompt

        return ptk_prompt("message> ", default=message).strip()
    except Exception:
        return Prompt.ask("message", default=message).strip()


# --------------------------------------------------------------------------- #
# Per-group review prompt
# --------------------------------------------------------------------------- #
def _prompt_group(
    index: int, total: int, group, changes: Changes, yes: bool,
    risky_paths: "frozenset[str]" = frozenset(),
) -> tuple[str, str]:
    """Render a group and ask what to do with it.

    Returns (action, payload) where action is one of "commit" (payload is the
    message), "skip", "quit", or "regroup" (payload is an optional instruction
    for re-grouping). The `d` choice prints the diff and re-asks.
    """
    _render_group(index, total, group)
    if yes:
        return "commit", group.message
    # A group touching a secret-flagged file defaults to "no" — opt in explicitly.
    has_secret = bool(risky_paths.intersection(group.files))
    if has_secret:
        console.print(
            "[bold red]⚠ this group touches a secret-flagged file[/bold red] "
            "[dim]— inspect with (d) before committing.[/dim]"
        )
    while True:
        choice = Prompt.ask(
            "[bold]Commit this?[/bold] "
            "[dim](y)es (n)o (e)dit (d)iff (r)egroup (q)uit[/dim]",
            choices=["y", "n", "e", "d", "r", "q"],
            default="n" if has_secret else "y",
            show_choices=False,
        )
        if choice == "q":
            console.print("[dim]Aborting remaining groups.[/dim]")
            return "quit", ""
        if choice == "n":
            console.print("[dim]Skipped.[/dim]")
            return "skip", ""
        if choice == "d":
            _render_group_diff(changes, group)
            continue
        if choice == "r":
            instr = Prompt.ask(
                "[dim]Regroup — one-line instruction (e.g. 'split tests out')[/dim]",
                default="",
            ).strip()
            if not instr:
                # temperature is 0, so an empty hint just reproduces the same
                # grouping — refuse rather than waste an identical API call.
                console.print(
                    "[dim]A hint is required, or the grouping won't change. Pick again.[/dim]"
                )
                continue
            return "regroup", instr
        if choice == "e":
            message = _edit_message(group.message)
            if not message:
                console.print("[dim]Empty message, skipped.[/dim]")
                return "skip", ""
            return "commit", message
        return "commit", group.message


def _final_summary(committed: int) -> None:
    if committed:
        console.print(f"\n[bold]Done — {committed} commit(s) created.[/bold]")
        console.print("[dim]ncomm never pushes. Review with `git log` and push when ready.[/dim]")
    else:
        console.print("\n[dim]No commits made.[/dim]")


# --------------------------------------------------------------------------- #
# Main run command
# --------------------------------------------------------------------------- #
@run_app.command()
def run(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Commit all proposed groups without prompting."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Show proposed commits; commit nothing."
    ),
    no_group: bool = typer.Option(
        False, "--no-group", help="Force a single commit covering all changes."
    ),
    only: Optional[List[str]] = typer.Option(
        None, "--only",
        help="Only consider paths matching this glob (repeatable). WIP files "
        "outside it stay in the working tree.",
    ),
    exclude: Optional[List[str]] = typer.Option(
        None, "--exclude",
        help="Ignore paths matching this glob (repeatable), e.g. --exclude '*.lock'.",
    ),
    staged: bool = typer.Option(
        False, "--staged",
        help="Write one commit for what you've already staged (git add), using "
        "the index as-is. Doesn't re-stage, so 'git add -p' selections are kept.",
    ),
    no_scan: bool = typer.Option(
        False, "--no-scan", help="Skip the secret/debug-leftover pre-commit scan."
    ),
    allow_secrets: bool = typer.Option(
        False, "--allow-secrets",
        help="Don't block --yes when the scan finds secret-like content.",
    ),
    pro: bool = typer.Option(
        False, "--pro", help=f"Use the stronger model ({PRO_MODEL}) for this request."
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m", help="Override the model id for this request."
    ),
    lang: str = typer.Option(
        os.environ.get("NCOMM_LANG", "en"),
        "--lang",
        help="Language for commit messages (e.g. en, zh).",
    ),
    style: Optional[bool] = typer.Option(
        None, "--style/--no-style",
        help="Show recent commits to the model to match repo style "
        "(default: config learn_style, on).",
    ),
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Group your working tree into Conventional Commits and commit them."""
    if staged and (only or exclude):
        err_console.print(
            "[red]--staged can't combine with --only/--exclude.[/red] In --staged "
            "mode ncomm commits the index as-is; curate it with `git add` instead."
        )
        raise typer.Exit(code=1)
    # --staged writes a single commit over the curated index; grouping it would
    # mean committing index subsets, which can't be done without disturbing a
    # partial `git add -p` selection.
    if staged:
        no_group = True

    cfg = load_config()
    if model:
        cfg.model = model
    elif pro:
        cfg.model = PRO_MODEL

    instruction = ""        # carries a regroup hint into the next round
    session_committed = 0
    regroup_rounds = 0
    gitignore_offered = False
    secrets_acknowledged = False
    while True:
        try:
            changes = collect_changes(only=only, exclude=exclude, staged=staged)
        except GitError as exc:
            err_console.print(f"[red]git error:[/red] {exc}")
            raise typer.Exit(code=1)

        if changes.is_empty:
            if session_committed:
                _final_summary(session_committed)
            elif staged:
                console.print("[dim]Nothing staged — `git add` some changes first.[/dim]")
            elif only or exclude:
                console.print("[dim]No changed files matched the --only/--exclude filter.[/dim]")
            else:
                console.print("[dim]Nothing to commit — working tree clean.[/dim]")
            return

        # Offer to .gitignore obvious untracked junk (once, interactively). On
        # acceptance, re-collect so the now-ignored files drop out and the
        # .gitignore change itself joins the set to be committed normally.
        if not staged and not yes and not dry_run and not gitignore_offered:
            gitignore_offered = True
            untracked = [fc.path for fc in changes.files if fc.status == "?"]
            candidates = scan.gitignore_candidates(untracked)
            # Ask the model to spot project-specific junk the rules can't know
            # about (only filenames are sent). Best-effort: failures are silent.
            model_set: set[str] = set()
            if cfg.has_key:
                with console.status("[dim]Checking for ignorable files…[/dim]", spinner="dots"):
                    for pat in suggest_gitignore(untracked, cfg):
                        if pat in candidates:
                            continue
                        covered = [p for p in untracked if fnmatch.fnmatch(p, pat)]
                        if covered:
                            candidates[pat] = covered
                            model_set.add(pat)
            if candidates:
                _render_gitignore_candidates(candidates, frozenset(model_set))
                if Prompt.ask(
                    "Add these to [bold].gitignore[/bold]?",
                    choices=["y", "n"], default="y",
                ) == "y":
                    added = scan.append_gitignore(changes.root, list(candidates))
                    console.print(
                        f"[green]Updated .gitignore[/green] (+{len(added)} pattern(s)); "
                        "re-reading changes…\n"
                    )
                    continue

        # Report a missing key before rendering the changes table, so the user
        # isn't shown their whole working tree only to be told they can't proceed.
        if not cfg.has_key:
            err_console.print(
                "[red]No DeepSeek API key found.[/red]\n"
                "Set one with:  [bold]ncomm config set-key[/bold]\n"
                "or:           [bold]export DEEPSEEK_API_KEY=sk-...[/bold]"
            )
            raise typer.Exit(code=1)

        _render_changes(changes)

        risky_paths: frozenset[str] = frozenset()
        if not no_scan and changes.findings:
            _render_findings(changes.findings)
            # Only high-confidence (structural) secrets gate; entropy hits advise.
            secrets = [
                f for f in changes.findings if f.kind == "secret" and f.confidence == "high"
            ]
            risky_paths = frozenset(f.path for f in secrets)
            if secrets and yes and not allow_secrets:
                err_console.print(
                    f"[red]Refusing to auto-commit with --yes:[/red] "
                    f"{len(secrets)} secret-like finding(s). Review without --yes, "
                    "or pass --allow-secrets to override."
                )
                raise typer.Exit(code=1)

        # The diff (secrets and all) is about to be sent to the model. Give an
        # interactive user the chance to stop before anything leaves the machine.
        if risky_paths and not yes and not secrets_acknowledged:
            if Prompt.ask(
                "[bold red]Secret-like content detected.[/bold red] "
                "Send the diff to the model anyway?",
                choices=["y", "n"], default="n",
            ) == "n":
                err_console.print(
                    "[dim]Aborted before sending. Remove the secrets "
                    "(or pass --no-scan) and re-run.[/dim]"
                )
                raise typer.Exit(code=1)
            secrets_acknowledged = True

        learn_style = cfg.learn_style if style is None else style
        style_examples = (
            recent_messages(STYLE_EXAMPLE_COUNT, cwd=changes.root) if learn_style else []
        )

        try:
            with console.status(f"[dim]Asking DeepSeek ({cfg.model})…[/dim]", spinner="dots"):
                groups = suggest_groups(
                    changes, cfg, no_group=no_group, lang=lang,
                    instruction=instruction, style_examples=style_examples,
                )
        except LLMError as exc:
            err_console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(code=1)

        changed_paths = {fc.path for fc in changes.files}
        err = _validate_groups(groups, changed_paths)
        if err:
            err_console.print(f"[red]Grouping looks wrong, aborting:[/red] {err}")
            err_console.print("[dim]Re-run, or use --no-group for a single commit.[/dim]")
            raise typer.Exit(code=1)

        total = len(groups)
        console.print(f"\n[bold green]Proposed {total} commit(s).[/bold green]\n")

        if dry_run:
            for i, g in enumerate(groups, 1):
                _render_group(i, total, g)
            return

        # In --staged mode unstaged changes are expected and intentionally left
        # out, so the "changed since analysis" check would only add noise.
        if not staged:
            surprises = ensure_clean_since(changed_paths, cwd=changes.root)
            if surprises:
                shown = ", ".join(sorted(surprises)[:5])
                more = f" (+{len(surprises) - 5} more)" if len(surprises) > 5 else ""
                err_console.print(
                    f"[yellow]note:[/yellow] {len(surprises)} file(s) changed since analysis "
                    f"and won't be part of any commit: {shown}{more}"
                )

        regroup_instruction = None
        for i, g in enumerate(groups, 1):
            action, message = _prompt_group(i, total, g, changes, yes, risky_paths)
            if action == "quit":
                break
            if action == "skip":
                continue
            if action == "regroup":
                regroup_instruction = message
                break

            try:
                if staged:
                    # Commit the index exactly as the user staged it — no add,
                    # no pathspec (a pathspec would pull in working-tree content
                    # and clobber a partial `git add -p` selection).
                    sha = commit(message, cwd=changes.root)
                else:
                    stage(g.files, cwd=changes.root)
                    # A renamed file's old path isn't its own changed entry, so carry
                    # its deletion into the commit pathspec or the rename is half-applied.
                    rename_olds = [changes.renames[p] for p in g.files if p in changes.renames]
                    sha = commit(message, cwd=changes.root, paths=g.files + rename_olds)
            except GitError as exc:
                err_console.print(f"[red]commit failed:[/red] {exc}")
                raise typer.Exit(code=1)
            console.print(f"[green]✓[/green] {sha}  {g.header}")
            session_committed += 1

        if regroup_instruction is not None:
            regroup_rounds += 1
            if regroup_rounds > MAX_REGROUP_ROUNDS:
                err_console.print(
                    f"[yellow]Reached the regroup limit ({MAX_REGROUP_ROUNDS}).[/yellow] "
                    "Use --no-group for one commit, or edit messages with 'e'."
                )
                break
            instruction = regroup_instruction
            console.print("[dim]Regrouping the remaining changes…[/dim]\n")
            continue

        break

    _final_summary(session_committed)


# --------------------------------------------------------------------------- #
# `ncomm config ...` subcommands
# --------------------------------------------------------------------------- #
@config_app.command("set-key")
def config_set_key(
    model: Optional[str] = typer.Option(
        None, "--model", "-m", help="Also set the default model."
    ),
) -> None:
    """Store your DeepSeek API key in the config file (mode 0600)."""
    key = Prompt.ask("DeepSeek API key", password=True)
    key = (key or "").strip()
    if not key:
        err_console.print("[yellow]No key entered, nothing changed.[/yellow]")
        raise typer.Exit(code=1)
    path = save_config({"api_key": key, "model": model})
    console.print(f"[green]Saved[/green] to {path}")


@config_app.command("set-model")
def config_set_model(
    model: str = typer.Argument(..., help="Model id, e.g. deepseek-v4-pro."),
) -> None:
    """Set the default model in the config file."""
    path = save_config({"model": model})
    console.print(f"[green]Saved[/green] model = {model}  ({path})")


@config_app.command("show")
def config_show() -> None:
    """Show the resolved configuration (the API key is masked)."""
    cfg = load_config()
    masked = "—"
    if cfg.api_key:
        k = cfg.api_key
        masked = f"{k[:4]}…{k[-4:]}" if len(k) > 8 else "set"
    console.print(f"config file : {config_path()}")
    console.print(f"api_key     : {masked}")
    console.print(f"base_url    : {cfg.base_url}")
    console.print(f"model       : {cfg.model}")
    console.print(f"learn_style : {cfg.learn_style}")
    console.print("[dim]ncomm will never run:[/dim] " + "; ".join(OUT_OF_SCOPE))


# --------------------------------------------------------------------------- #
# Entry point: route `ncomm config ...` to config_app, everything else to run.
# --------------------------------------------------------------------------- #
def app() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "config":
        config_app(args=argv[1:], prog_name="ncomm config")
    else:
        run_app(args=argv, prog_name="ncomm")


if __name__ == "__main__":  # pragma: no cover
    app()
