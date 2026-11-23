"""The run-error classifier that powers 'why did this test not run?' hints."""

from __future__ import annotations

import pytest

from dvp.testing_engine.service import _error_hint


@pytest.mark.parametrize(
    "error,needle",
    [
        ("Connection timed out after 30s", "connection"),
        ("could not connect to host snowflake.acme.com", "connection"),
        ("getaddrinfo failed: name or service not known", "connection"),
        ("SQL access control error: permission denied on table ORDERS", "permissions"),
        ("User is not authorized to perform: SELECT", "permissions"),
        ("Insufficient privileges to operate on schema RAW", "permissions"),
        ("SQL compilation error: Object 'RAW.ORDERS' does not exist", "renamed or dropped"),
        ("invalid identifier 'C_MISSING'", "renamed or dropped"),
        ("no such table: SRC", "renamed or dropped"),
        ("SQL compilation error: syntax error line 1 at position 7", "SQL error"),
    ],
)
def test_error_hint_classifies_common_failures(error, needle):
    hint = _error_hint(error)
    assert hint is not None, f"expected a hint for: {error}"
    assert needle.lower() in hint.lower()


def test_error_hint_is_none_for_empty_or_unknown():
    assert _error_hint(None) is None
    assert _error_hint("") is None
    assert _error_hint("some totally unrecognised failure xyz") is None
