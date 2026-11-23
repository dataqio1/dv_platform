"""IdentityProvider — how a request authenticates to the platform.

local_nopassword: desktop mode, a single auto-provisioned user, no login (D2).
oidc: SaaS/server, enterprise SSO per org (stub here; wire real OIDC later).
Both resolve to the same TenantContext downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str
    org_id: str
    # LDAP/SSO group memberships — each maps to a tenant (JIT-provisioned). Empty for the
    # single-user desktop provider.
    groups: tuple[str, ...] = ()
    display_name: str | None = None
    external_sub: str | None = None


class IdentityProvider(Protocol):
    def authenticate(self, token: str | None) -> Identity: ...


class LocalNoPasswordIdentity:
    """Returns the single local user regardless of token. Desktop only."""

    def __init__(self, user_id: str, email: str, org_id: str) -> None:
        self._identity = Identity(user_id=user_id, email=email, org_id=org_id)

    def authenticate(self, token: str | None) -> Identity:  # noqa: ARG002
        return self._identity


class OidcIdentityProvider:
    """Placeholder for enterprise SSO. Verifies a platform JWT minted after OIDC
    login and extracts (user_id, org_id). Not implemented in the scaffold."""

    def __init__(self, issuer: str, client_id: str) -> None:
        self.issuer = issuer
        self.client_id = client_id

    def authenticate(self, token: str | None) -> Identity:
        from dvp.core.errors import AuthError

        raise AuthError("OIDC identity provider not yet implemented in this scaffold")
