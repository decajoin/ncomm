"""Tests for ncomm.llm — JSON parsing into CommitGroup objects."""

import json

import pytest

import ncomm.llm as llm
from ncomm.config import Config
from ncomm.gitops import Changes
from ncomm.llm import CommitGroup, LLMError, _style_message, _user_message, parse_groups


def _cfg():
    return Config(api_key="sk-x", base_url="https://api.example.com", model="m")


class _FakeResp:
    status_code = 200

    def __init__(self, patterns):
        self._patterns = patterns

    def json(self):
        content = json.dumps({"patterns": self._patterns})
        return {"choices": [{"message": {"content": content}}]}


def test_suggest_gitignore_filters_source_and_lock(monkeypatch):
    monkeypatch.setattr(
        llm.httpx, "post",
        lambda *a, **k: _FakeResp(["dist/", "*.log", "app.py", "pyproject.toml", "models/*.ckpt"]),
    )
    out = llm.suggest_gitignore(["dist/x", "a.log", "app.py", "models/m.ckpt"], _cfg())
    assert "dist/" in out and "*.log" in out and "models/*.ckpt" in out
    assert "app.py" not in out          # source dropped
    assert "pyproject.toml" not in out  # committed config dropped


def test_suggest_gitignore_no_key_or_empty_returns_empty():
    assert llm.suggest_gitignore(["x.log"], Config(api_key=None, base_url="u", model="m")) == []
    assert llm.suggest_gitignore([], _cfg()) == []


def test_suggest_gitignore_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise llm.httpx.HTTPError("network down")

    monkeypatch.setattr(llm.httpx, "post", boom)
    assert llm.suggest_gitignore(["x.log"], _cfg()) == []


def test_style_message_empty_is_none():
    assert _style_message(None) is None
    assert _style_message([]) is None
    assert _style_message(["  ", ""]) is None


def test_style_message_lists_subjects():
    msg = _style_message(["feat(auth): add login", "fix: typo"])
    assert msg["role"] == "system"
    assert "feat(auth): add login" in msg["content"]
    assert "fix: typo" in msg["content"]


def test_user_message_includes_regroup_instruction():
    changes = Changes(branch="main", diff_bundle="diff goes here")
    msg = _user_message(changes, no_group=False, lang="en", instruction="split tests out")
    assert "split tests out" in msg
    # No instruction -> no instruction line.
    plain = _user_message(changes, no_group=False, lang="en")
    assert "instruction" not in plain.lower()


def _group(**kw):
    base = {
        "type": "feat",
        "summary": "add thing",
        "scope": "",
        "body": "",
        "files": ["a.py"],
        "rationale": "",
    }
    base.update(kw)
    return base


def test_parse_single_group():
    groups = parse_groups({"groups": [_group()]})
    assert len(groups) == 1
    assert groups[0].header == "feat: add thing"
    assert groups[0].message == "feat: add thing"


def test_parse_group_with_scope_and_body():
    g = parse_groups(
        {"groups": [_group(scope="auth", body="Wires OAuth2.\nSecond line.")]}
    )[0]
    assert g.header == "feat(auth): add thing"
    assert g.message == "feat(auth): add thing\n\nWires OAuth2.\nSecond line."


def test_parse_multiple_groups():
    groups = parse_groups(
        {"groups": [_group(files=["a.py"]), _group(type="fix", summary="b", files=["b.py"])]}
    )
    assert len(groups) == 2
    assert groups[1].type == "fix"


def test_parse_rejects_missing_groups():
    with pytest.raises(LLMError):
        parse_groups({"groups": []})


def test_parse_rejects_non_object():
    with pytest.raises(LLMError):
        parse_groups(["nope"])


def test_parse_rejects_group_without_files():
    with pytest.raises(LLMError):
        parse_groups({"groups": [_group(files=[])]})


def test_parse_rejects_missing_type():
    with pytest.raises(LLMError):
        parse_groups({"groups": [_group(type="")]})


def test_group_header_strips_empty_scope():
    g = CommitGroup(type="chore", summary="bump deps", scope="")
    assert g.header == "chore: bump deps"


@pytest.mark.parametrize("bad_scope", ["None", "null", "NONE", "-", "n/a"])
def test_parse_strips_null_literal_scope(bad_scope):
    g = parse_groups({"groups": [_group(scope=bad_scope)]})[0]
    assert g.scope == ""
    assert g.header == "feat: add thing"
