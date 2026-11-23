"""Snowflake auth policy: key-pair and SSO (external browser) only — never password."""

from __future__ import annotations

import pytest

from dvp.connectors.snowflake.auth import build_connect_kwargs

COORDS = {"account": "xy12345.us-east-1", "user": "SVC", "warehouse": "WH"}


def test_external_browser_sso_no_password():
    kw = build_connect_kwargs(COORDS, "external_browser", None)
    assert kw["authenticator"] == "externalbrowser"
    assert kw["client_store_temporary_credential"] is True  # cached → one-time prompt
    assert "password" not in kw


def test_password_is_rejected():
    with pytest.raises(ValueError, match="unsupported auth_method"):
        build_connect_kwargs(COORDS, "password", {"password": "nope"})


def test_key_pair_requires_private_key():
    pytest.importorskip("cryptography")
    with pytest.raises(ValueError, match="private_key"):
        build_connect_kwargs(COORDS, "key_pair", {})


def test_key_pair_happy_path():
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    from cryptography.hazmat.primitives import serialization

    key = crypto.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    kw = build_connect_kwargs(COORDS, "key_pair", {"private_key": pem})
    assert isinstance(kw["private_key"], bytes)  # loaded + re-encoded as DER
    assert "password" not in kw and "authenticator" not in kw
