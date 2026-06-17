"""Tests for ncomm.safety."""

from ncomm.safety import OUT_OF_SCOPE, amend_requires_typed_yes, is_out_of_scope


def test_out_of_scope_push_detected():
    assert is_out_of_scope("git push origin main")


def test_out_of_scope_force_push_detected():
    assert is_out_of_scope("git push --force origin main")


def test_out_of_scope_reset_hard_detected():
    assert is_out_of_scope("git reset --hard HEAD~1")


def test_out_of_scope_rebase_detected():
    assert is_out_of_scope("git rebase main")


def test_in_scope_add_not_flagged():
    assert not is_out_of_scope("git add src/foo.py")


def test_in_scope_commit_not_flagged():
    assert not is_out_of_scope("git commit -m feat: x")


def test_amend_requires_full_yes():
    assert amend_requires_typed_yes("yes")


def test_amend_rejects_short_confirmation():
    assert not amend_requires_typed_yes("y")
    assert not amend_requires_typed_yes("")


def test_out_of_scope_list_nonempty():
    assert OUT_OF_SCOPE  # the contract list is populated
