"""LDAP directory port + identity provider.

Multi-tenancy is driven by LDAP groups: authenticating a user yields the groups they belong
to, and each group maps to a tenant (JIT-provisioned downstream). The LDAP *binding* is kept
behind an `LdapDirectory` port so the provisioning/mapping logic is testable without a live
server (`StaticDirectory`), while production binds a real server via `Ldap3Directory` (lazy
`ldap3` import, the `ldap` extra).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from dvp.abstractions.identity import Identity
from dvp.core.errors import AuthError


@dataclass(frozen=True)
class DirectoryUser:
    uid: str
    email: str
    display_name: str | None
    groups: tuple[str, ...]


class LdapDirectory(Protocol):
    def authenticate(self, token: str | None) -> DirectoryUser:
        """Validate the credential/token and return the user + their group memberships."""
        ...


@dataclass
class StaticDirectory:
    """In-memory directory for local mode and tests. `users` maps token → DirectoryUser.

    A token here is an opaque bearer the deployment mints after an LDAP bind (or, in tests,
    a fixed string). No passwords are stored."""

    users: dict[str, DirectoryUser] = field(default_factory=dict)

    def authenticate(self, token: str | None) -> DirectoryUser:
        if not token:
            raise AuthError("missing bearer token")
        token = token.removeprefix("Bearer ").strip()
        user = self.users.get(token)
        if user is None:
            raise AuthError("unknown or expired token")
        return user


class Ldap3Directory:
    """Production directory — binds the user to the LDAP server and reads their groups.

    `token` is "<username>:<password>" (or a pre-validated username when bind is done
    upstream). Requires the `ldap` extra (python-ldap/ldap3)."""

    def __init__(
        self,
        url: str,
        user_dn_template: str,
        group_base_dn: str,
        group_member_attr: str = "member",
        group_name_attr: str = "cn",
    ) -> None:
        self.url = url
        self.user_dn_template = user_dn_template
        self.group_base_dn = group_base_dn
        self.group_member_attr = group_member_attr
        self.group_name_attr = group_name_attr

    def authenticate(self, token: str | None) -> DirectoryUser:  # pragma: no cover - needs server
        try:
            import ldap3
        except ImportError as exc:
            raise RuntimeError(
                "LDAP needs the 'ldap' extra: pip install 'dv-platform[ldap]'"
            ) from exc
        if not token or ":" not in token:
            raise AuthError("LDAP token must be '<username>:<password>'")
        username, password = token.split(":", 1)
        user_dn = self.user_dn_template.format(username=username)
        server = ldap3.Server(self.url, get_info=ldap3.NONE)
        conn = ldap3.Connection(server, user=user_dn, password=password, auto_bind=True)
        conn.search(
            self.group_base_dn,
            f"({self.group_member_attr}={user_dn})",
            attributes=[self.group_name_attr],
        )
        groups = tuple(str(e[self.group_name_attr]) for e in conn.entries)
        email = username if "@" in username else f"{username}@ldap"
        return DirectoryUser(uid=username, email=email, display_name=username, groups=groups)


class LdapIdentityProvider:
    """IdentityProvider backed by an LdapDirectory — returns an Identity carrying the user's
    LDAP groups. Tenant/org ids are resolved by the provisioner downstream, so `user_id`/
    `org_id` are left empty here."""

    def __init__(self, directory: LdapDirectory) -> None:
        self.directory = directory

    def authenticate(self, token: str | None) -> Identity:
        u = self.directory.authenticate(token)
        if not u.groups:
            raise AuthError("user has no LDAP groups → no tenant access")
        return Identity(
            user_id="",
            email=u.email,
            org_id="",
            groups=u.groups,
            display_name=u.display_name,
            external_sub=u.uid,
        )
