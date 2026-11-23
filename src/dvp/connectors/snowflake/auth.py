"""Snowflake authentication strategies → connector kwargs (docs/design/03).

Supports:
  - key_pair          : service accounts / scheduled runs (no browser).
  - oauth_sso         : Snowflake OAuth. Three sub-modes, in priority order:
        * access_token present                  -> authenticator='oauth' (no browser)
        * oauth_client_id/secret present        -> authorization-code flow
                                                   (browser ONCE, token cached by the
                                                   driver via client_store_temporary_credential)
        * otherwise                             -> externalbrowser fallback
  - external_browser  : desktop SSO; browser once, ID token cached locally.

The "no webpage per request" guarantee: the driver caches the token/ID token so
repeated connects reuse it until expiry (then a silent refresh, or one re-auth).
"""

from __future__ import annotations

from typing import Any


def build_connect_kwargs(
    coordinates: dict, auth_method: str, secret: dict | None
) -> dict[str, Any]:
    secret = secret or {}
    account = coordinates.get("account")
    if not account:
        raise ValueError("connection coordinates must include 'account'")

    kwargs: dict[str, Any] = {
        "account": account,
        "client_session_keep_alive": True,
        "session_parameters": {"QUERY_TAG": "dvp"},
    }
    for key in ("user", "role", "warehouse", "database", "schema", "host"):
        if coordinates.get(key):
            kwargs[key] = coordinates[key]

    if auth_method == "key_pair":
        kwargs["private_key"] = _load_private_key(secret)
        return kwargs

    if auth_method == "external_browser":
        kwargs["authenticator"] = "externalbrowser"
        kwargs["client_store_temporary_credential"] = True
        return kwargs

    if auth_method == "oauth_sso":
        if secret.get("access_token"):
            # Pre-obtained OAuth access token — no browser, ever.
            kwargs["authenticator"] = "oauth"
            kwargs["token"] = secret["access_token"]
            return kwargs
        if secret.get("oauth_client_id"):
            # Authorization-code flow handled by the driver: browser ONCE, then the
            # driver caches + refreshes tokens (client_store_temporary_credential).
            kwargs["authenticator"] = "OAUTH_AUTHORIZATION_CODE"
            kwargs["oauth_client_id"] = secret["oauth_client_id"]
            kwargs["oauth_client_secret"] = secret.get("oauth_client_secret")
            kwargs["client_store_temporary_credential"] = True
            for opt in ("oauth_authorization_url", "oauth_token_request_url", "oauth_redirect_uri"):
                if secret.get(opt):
                    kwargs[opt] = secret[opt]
            if coordinates.get("oauth_scope"):
                kwargs["oauth_scope"] = coordinates["oauth_scope"]
            return kwargs
        # No token and no client creds: fall back to browser SSO (still OAuth under
        # the hood for most Snowflake SSO setups), cached so it's a one-time prompt.
        kwargs["authenticator"] = "externalbrowser"
        kwargs["client_store_temporary_credential"] = True
        return kwargs

    raise ValueError(f"unsupported auth_method: {auth_method}")


def _load_private_key(secret: dict) -> bytes:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    pem = secret.get("private_key")
    if not pem:
        raise ValueError("key_pair auth requires 'private_key' in the secret")
    if isinstance(pem, str):
        pem = pem.encode()
    passphrase = secret.get("passphrase")
    key = serialization.load_pem_private_key(
        pem, password=passphrase.encode() if passphrase else None, backend=default_backend()
    )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
