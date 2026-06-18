"""Tests for ncomm.safety."""

from ncomm.safety import OUT_OF_SCOPE


def test_out_of_scope_list_nonempty():
    assert OUT_OF_SCOPE  # the contract list is populated


def test_out_of_scope_covers_the_dangerous_ops():
    blob = " ".join(OUT_OF_SCOPE).lower()
    for marker in ("push", "reset", "rebase", "--no-verify"):
        assert marker in blob, f"contract should mention {marker}"
