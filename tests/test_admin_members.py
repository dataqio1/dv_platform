"""Admin: group membership management + system info (no mocks, real API via client)."""

from __future__ import annotations

API = "/api/v1"


def _members(client):
    gid = client.get(f"{API}/me").json()["data"]["group_id"]
    return gid, client.get(f"{API}/groups/{gid}/members").json()["data"]


def test_list_includes_bootstrapped_admin(client):
    _gid, members = _members(client)
    assert len(members) >= 1
    assert any(m["role"] == "group_admin" for m in members)
    assert all("email" in m and "user_id" in m for m in members)


def test_add_change_role_and_remove(client):
    gid, _ = _members(client)

    added = client.post(
        f"{API}/groups/{gid}/members", json={"email": "analyst@acme.io", "role": "editor"}
    )
    assert added.status_code == 201
    uid = added.json()["data"]["user_id"]
    assert added.json()["data"]["role"] == "editor"

    # duplicate add is rejected
    dup = client.post(f"{API}/groups/{gid}/members", json={"email": "analyst@acme.io", "role": "viewer"})
    assert dup.status_code >= 400

    # change role
    upd = client.put(f"{API}/groups/{gid}/members/{uid}", json={"role": "viewer"})
    assert upd.status_code == 200
    assert upd.json()["data"]["role"] == "viewer"

    # remove
    rm = client.delete(f"{API}/groups/{gid}/members/{uid}")
    assert rm.status_code == 204
    _, after = _members(client)
    assert all(m["user_id"] != uid for m in after)


def test_cannot_demote_or_remove_last_admin(client):
    gid, members = _members(client)
    admin = next(m for m in members if m["role"] == "group_admin")
    uid = admin["user_id"]

    demote = client.put(f"{API}/groups/{gid}/members/{uid}", json={"role": "viewer"})
    assert demote.status_code >= 400
    assert "last group admin" in demote.text.lower()

    remove = client.delete(f"{API}/groups/{gid}/members/{uid}")
    assert remove.status_code >= 400


def test_unknown_role_rejected(client):
    gid, _ = _members(client)
    r = client.post(f"{API}/groups/{gid}/members", json={"email": "x@acme.io", "role": "superuser"})
    assert r.status_code >= 400


def test_path_group_must_match_active(client):
    r = client.get(f"{API}/groups/some-other-group/members")
    assert r.status_code >= 400


def test_system_info(client):
    info = client.get(f"{API}/system/info").json()["data"]
    assert info["profile"] == "local"
    assert info["is_local"] is True
    assert info["identity_provider"] == "local_nopassword"
    assert info["database"] in ("sqlite", "postgres", "unknown")
    assert "scheduler" in info and "enabled" in info["scheduler"]
    assert info["version"]
