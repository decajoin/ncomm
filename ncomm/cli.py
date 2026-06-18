"""Command-line interface for ncomm."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import (
    PRO_MODEL,
    config_path,
    load_config,
    save_config,
)
from .gitops import Changes, GitError, collect_changes, commit, stage
from .llm import LLMError, suggest_groups
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
        try:
            import subprocess

            subprocess.run([editor, tmp_path], check=True)
            new = Path(tmp_path).read_text(encoding="utf-8")
        except (subprocess.CalledProcessError, OSError) as exc:
            err_console.print(f"[yellow]editor failed: {exc}; using prompt.[/yellow]")
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
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Group your working tree into Conventional Commits and commit them."""
    try:
        changes = collect_changes()
    except GitError as exc:
        err_console.print(f"[red]git error:[/red] {exc}")
        raise typer.Exit(code=1)

    if changes.is_empty:
        console.print("[dim]Nothing to commit — working tree clean.[/dim]")
        raise typer.Exit(code=0)

    _render_changes(changes)

    cfg = load_config()
    if not cfg.has_key:
        err_console.print(
            "[red]No DeepSeek API key found.[/red]\n"
            "Set one with:  [bold]ncomm config set-key[/bold]\n"
            "or:           [bold]export DEEPSEEK_API_KEY=sk-...[/bold]"
        )
        raise typer.Exit(code=1)

    if model:
        cfg.model = model
    elif pro:
        cfg.model = PRO_MODEL

    try:
        with console.status(f"[dim]Asking DeepSeek ({cfg.model})…[/dim]", spinner="dots"):
            groups = suggest_groups(changes, cfg, no_group=no_group, lang=lang)
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

    committed = 0
    for i, g in enumerate(groups, 1):
        if yes:
            message = g.message
            _render_group(i, total, g)
        else:
            _render_group(i, total, g)
            choice = Prompt.ask(
                "[bold]Commit this?[/bold]",
                choices=["y", "n", "e", "q"],
                default="y",
            )
            if choice == "q":
                console.print("[dim]Aborting remaining groups.[/dim]")
                break
            if choice == "n":
                console.print("[dim]Skipped.[/dim]")
                continue
            if choice == "e":
                message = _edit_message(g.message)
                if not message:
                    console.print("[dim]Empty message, skipped.[/dim]")
                    continue
            else:
                message = g.message

        try:
            stage(g.files, cwd=changes.root)
            # A renamed file's old path isn't its own changed entry, so carry
            # its deletion into the commit pathspec or the rename is half-applied.
            rename_olds = [changes.renames[p] for p in g.files if p in changes.renames]
            sha = commit(message, cwd=changes.root, paths=g.files + rename_olds)
        except GitError as exc:
            err_console.print(f"[red]commit failed:[/red] {exc}")
            raise typer.Exit(code=1)
        console.print(f"[green]✓[/green] {sha}  {g.header}")
        committed += 1

    if committed:
        console.print(f"\n[bold]Done — {committed}/{total} commit(s) created.[/bold]")
        console.print("[dim]ncomm never pushes. Review with `git log` and push when ready.[/dim]")
    else:
        console.print("\n[dim]No commits made.[/dim]")


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
