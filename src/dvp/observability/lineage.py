"""Table lineage past one hop.

Edges are stored one parent → child at a time. A table page walks those edges
so a bronze table names the silver model and the gold model that sits on it.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from dvp.core.errors import ValidationError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.models import LineageEdge
from dvp.db.session import MetadataStore
from dvp.services import audit

_MAX_HOPS = 5


class LineageService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def add_edges(self, ctx: TenantContext, edges: list[dict]) -> dict:
        ctx.require(Permission.WRITE)
        if not edges:
            raise ValidationError("no lineage edges")
        added = 0
        with self.store.session(ctx) as s:
            have = {
                (_key(e.upstream_ref), _key(e.downstream_ref))
                for e in s.scalars(
                    select(LineageEdge).where(LineageEdge.group_id == ctx.group_id)
                ).all()
            }
            for raw in edges:
                up = str(raw.get("upstream") or "").strip()
                down = str(raw.get("downstream") or "").strip()
                if not up or not down:
                    raise ValidationError("an edge needs an upstream and a downstream table")
                if _key(up) == _key(down):
                    raise ValidationError("an edge cannot point a table at itself")
                pair = (_key(up), _key(down))
                if pair in have:
                    continue
                s.add(
                    LineageEdge(
                        org_id=ctx.org_id,
                        group_id=ctx.group_id,
                        upstream_ref=up[:512],
                        downstream_ref=down[:512],
                    )
                )
                have.add(pair)
                added += 1
            audit.record(
                s,
                ctx,
                "lineage.edges",
                entity_type="lineage",
                detail={"added": added, "submitted": len(edges)},
            )
        return {"added": added, "submitted": len(edges)}

    def around(self, ctx: TenantContext, table_ref: str) -> dict:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            rows = s.scalars(
                select(LineageEdge).where(LineageEdge.group_id == ctx.group_id)
            ).all()
        pairs = [(r.upstream_ref, r.downstream_ref) for r in rows]
        downstream = _walk(pairs, table_ref, direction="down")
        upstream = _walk(pairs, table_ref, direction="up")
        return {
            "status": "mapped" if downstream or upstream else "unmapped",
            "nodes": [{"name": table_ref.split(".")[-1], "type": "SOURCE"}],
            "upstream": upstream,
            "downstream": downstream,
        }


def _key(ref: str) -> str:
    return ref.strip().upper()


def _same(a: str, b: str) -> bool:
    left, right = _key(a), _key(b)
    return left == right or left.split(".")[-1] == right.split(".")[-1]


def _walk(pairs: list[tuple[str, str]], start: str, *, direction: str) -> list[dict]:
    children: dict[str, list[str]] = defaultdict(list)
    for upstream, downstream in pairs:
        parent, child = (upstream, downstream) if direction == "down" else (downstream, upstream)
        children[_key(parent)].append(child)
        children[_key(parent).split(".")[-1]].append(child)
    seen = {_key(start), _key(start).split(".")[-1]}
    found: list[dict] = []
    frontier = [(start, 0)]
    while frontier:
        node, hops = frontier.pop(0)
        if hops >= _MAX_HOPS:
            continue
        nxts = children.get(_key(node), [])
        if "." in node:
            nxts = nxts + children.get(_key(node).split(".")[-1], [])
        for nxt in nxts:
            token = _key(nxt)
            if token in seen or any(_same(nxt, item["name"]) for item in found):
                continue
            if _same(nxt, start):
                seen.add(token)
                continue
            seen.add(token)
            seen.add(token.split(".")[-1])
            found.append({"name": nxt, "hops": hops + 1})
            frontier.append((nxt, hops + 1))
    return found
