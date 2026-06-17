"""Tests for ncomm.llm — JSON parsing into CommitGroup objects."""

import pytest

from ncomm.llm import CommitGroup, LLMError, parse_groups


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
