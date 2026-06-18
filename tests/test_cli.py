"""Tests for ncomm.cli interactive helpers (prompt driven via monkeypatch)."""

import subprocess

from typer.testing import CliRunner

import ncomm.cli as cli
from ncomm.gitops import Changes
from ncomm.llm import CommitGroup

runner = CliRunner()


def test_staged_rejects_only_and_exclude():
    # The guard fires before any git/LLM work, so this needs no repo or key.
    result = runner.invoke(cli.run_app, ["--staged", "--only", "src/**"])
    assert result.exit_code == 1
    assert "can't combine" in result.output


class _FakeStdin:
    def isatty(self):
        return True


class _FakePrompt:
    """Stand-in for rich.prompt.Prompt.ask that replays a fixed answer list."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def ask(self, *args, **kwargs):
        self.calls += 1
        return self.answers.pop(0)


def _group():
    return CommitGroup(type="feat", summary="do thing", files=["a.py"])


def _drive(monkeypatch, answers):
    fake = _FakePrompt(answers)
    monkeypatch.setattr(cli.Prompt, "ask", fake.ask)
    action, payload = cli._prompt_group(1, 1, _group(), Changes(branch="main"), yes=False)
    return action, payload, fake


def test_prompt_accept(monkeypatch):
    action, payload, _ = _drive(monkeypatch, ["y"])
    assert action == "commit"
    assert payload == _group().message


def test_prompt_skip_and_quit(monkeypatch):
    assert _drive(monkeypatch, ["n"])[0] == "skip"
    assert _drive(monkeypatch, ["q"])[0] == "quit"


def test_regroup_rejects_empty_hint_then_accepts(monkeypatch):
    # 'r' then empty hint -> re-ask; 'r' then a real hint -> regroup.
    action, payload, fake = _drive(monkeypatch, ["r", "", "r", "split tests out"])
    assert action == "regroup"
    assert payload == "split tests out"
    assert fake.calls == 4  # proves the empty hint forced another round


def test_yes_mode_commits_without_prompting(monkeypatch):
    # In --yes mode no prompt is consulted at all.
    def boom(*a, **k):
        raise AssertionError("Prompt.ask must not be called in --yes mode")

    monkeypatch.setattr(cli.Prompt, "ask", boom)
    action, payload = cli._prompt_group(1, 1, _group(), Changes(branch="main"), yes=True)
    assert action == "commit"


def test_edit_message_splits_editor_with_flags(monkeypatch):
    monkeypatch.setenv("EDITOR", "fakeed --wait")
    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin())

    captured = {}

    def fake_run(cmd, check):
        captured["cmd"] = cmd
        with open(cmd[-1], "w", encoding="utf-8") as fh:
            fh.write("edited message\n")
        return None

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = cli._edit_message("original")

    # "fakeed --wait" must become two argv tokens, not one filename.
    assert captured["cmd"][:2] == ["fakeed", "--wait"]
    assert out == "edited message"
