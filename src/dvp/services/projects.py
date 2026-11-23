"""ProjectService — group test cases (compare jobs + monitors) for management + reporting."""

from __future__ import annotations

from sqlalchemy import select

from dvp.core.errors import NotFoundError
from dvp.core.tenancy import Permission, TenantContext
from dvp.db.base import utcnow
from dvp.db.models import CompareJob, Monitor, Project
from dvp.db.session import MetadataStore
from dvp.services import audit


class ProjectService:
    def __init__(self, store: MetadataStore) -> None:
        self.store = store

    def create(
        self,
        ctx: TenantContext,
        *,
        name: str,
        description: str | None = None,
        tags: list | None = None,
        variables: dict | None = None,
    ) -> dict:
        # Creating a project is an admin capability (MANAGE_PROJECTS), not something every
        # editor can do — editors work within projects an admin has set up.
        ctx.require(Permission.MANAGE_PROJECTS)
        with self.store.session(ctx) as s:
            p = Project(
                org_id=ctx.org_id,
                group_id=ctx.group_id,
                name=name,
                description=description,
                tags=tags or [],
                variables=variables or {},
                created_by=ctx.user_id,
            )
            s.add(p)
            s.flush()
            audit.record(s, ctx, "project.create", entity_type="project", entity_id=p.id)
            return _dict(p)

    def update(
        self,
        ctx: TenantContext,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        tags: list | None = None,
        variables: dict | None = None,
    ) -> dict:
        ctx.require(Permission.WRITE)
        with self.store.session(ctx) as s:
            p = self._load(s, ctx, project_id)
            if name is not None:
                p.name = name
            if description is not None:
                p.description = description
            if tags is not None:
                p.tags = tags
            if variables is not None:
                p.variables = variables
            audit.record(s, ctx, "project.update", entity_type="project", entity_id=p.id)
            return _dict(p)

    def list(self, ctx: TenantContext, *, include_archived: bool = False) -> list[dict]:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            q = select(Project).where(Project.group_id == ctx.group_id)
            if not include_archived:
                q = q.where(Project.archived_at.is_(None))
            projects = s.scalars(q.order_by(Project.created_at.desc())).all()
            # Per-project test-case counts + health (pass/diff/fail of the LATEST result of each
            # test case), computed in two group-wide queries rather than N-per-project.
            health = _project_health(s, ctx.group_id)
            out = []
            for p in projects:
                d = _dict(p)
                d["compare_count"] = _test_case_count(s, p.id)
                d["monitor_count"] = (
                    s.scalar(select(_count_m()).where(Monitor.project_id == p.id)) or 0
                )
                d["health"] = health.get(p.id, _empty_health())
                out.append(d)
            return out

    def get(self, ctx: TenantContext, project_id: str) -> dict:
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            return _dict(self._load(s, ctx, project_id))

    def test_cases(self, ctx: TenantContext, project_id: str) -> dict:
        """All test cases in the project: compare jobs + monitors (DQ)."""
        ctx.require(Permission.READ)
        with self.store.session(ctx) as s:
            self._load(s, ctx, project_id)
            # Project ownership is already verified by _load, but filter the children by group_id
            # too (defense-in-depth): the listing stays tenant-safe even if a CompareJob/Monitor
            # ever ends up with a project_id/group_id mismatch.
            compares = s.scalars(
                select(CompareJob)
                .where(CompareJob.project_id == project_id, CompareJob.group_id == ctx.group_id)
                .order_by(CompareJob.created_at.desc())
            ).all()
            monitors = s.scalars(
                select(Monitor)
                .where(Monitor.project_id == project_id, Monitor.group_id == ctx.group_id)
                .order_by(Monitor.created_at.desc())
            ).all()
            return {
                "compares": [
                    {
                        "id": c.id,
                        "name": c.name,
                        "kind": c.kind,
                        "status": c.status,
                        "source_system": c.source_system,
                        "layer": c.layer,
                        "tags": c.tags or [],
                    }
                    for c in compares
                ],
                "monitors": [
                    {
                        "id": m.id,
                        "name": m.name,
                        "source_system": m.source_system,
                        "layer": m.layer,
                        "tags": m.tags or [],
                    }
                    for m in monitors
                ],
            }

    def delete(self, ctx: TenantContext, project_id: str) -> dict:
        """Soft-delete (archive) the project. Its test cases are retained and hidden with
        it — never orphaned — so it can be restored intact. Admin-only (MANAGE_PROJECTS)."""
        ctx.require(Permission.MANAGE_PROJECTS)
        with self.store.session(ctx) as s:
            p = self._load(s, ctx, project_id)
            if p.archived_at is None:
                p.archived_at = utcnow()
            audit.record(s, ctx, "project.archive", entity_type="project", entity_id=project_id)
            return _dict(p)

    def restore(self, ctx: TenantContext, project_id: str) -> dict:
        """Un-archive a soft-deleted project (and, implicitly, its test cases)."""
        ctx.require(Permission.MANAGE_PROJECTS)
        with self.store.session(ctx) as s:
            p = self._load(s, ctx, project_id)
            p.archived_at = None
            audit.record(s, ctx, "project.restore", entity_type="project", entity_id=project_id)
            return _dict(p)

    def _load(self, s, ctx: TenantContext, project_id: str) -> Project:  # noqa: ANN001
        p = s.get(Project, project_id)
        if p is None or p.group_id != ctx.group_id:
            raise NotFoundError("project not found", details={"id": project_id})
        return p


def _count():  # noqa: ANN001 - small helpers to keep imports local
    from sqlalchemy import func

    return func.count(CompareJob.id)


def _test_case_count(s, project_id: str) -> int:  # noqa: ANN001
    """Compare TEST CASES in a project. A multi-dataset compare (a batch) is ONE test case — not
    one per pair — so this counts standalone jobs + distinct batches, matching the Test cases tab
    (otherwise the project header would over-count a 5-pair batch as 5)."""
    from sqlalchemy import func

    standalone = s.scalar(
        select(func.count(CompareJob.id)).where(
            CompareJob.project_id == project_id, CompareJob.batch_id.is_(None)
        )
    ) or 0
    batches = s.scalar(
        select(func.count(func.distinct(CompareJob.batch_id))).where(
            CompareJob.project_id == project_id, CompareJob.batch_id.isnot(None)
        )
    ) or 0
    return standalone + batches


def _count_m():  # noqa: ANN001
    from sqlalchemy import func

    return func.count(Monitor.id)


def _empty_health() -> dict:
    return {"passing": 0, "differences": 0, "failed": 0, "running": 0, "not_run": 0, "pass_rate": 0.0}


def _project_health(s, group_id: str) -> dict[str, dict]:  # noqa: ANN001
    """Per-project health from the LATEST result of each test case (a batch counts once, aggregated
    over its pairs). Two group-wide queries: all compare jobs + their result match verdicts."""
    from dvp.db.models import CompareResult

    jobs = s.execute(
        select(CompareJob.id, CompareJob.project_id, CompareJob.batch_id, CompareJob.status)
        .where(CompareJob.group_id == group_id)
    ).all()
    match_by_job = {
        cid: (summ or {}).get("match")
        for cid, summ in s.execute(
            select(CompareResult.compare_job_id, CompareResult.summary)
            .join(CompareJob, CompareJob.id == CompareResult.compare_job_id)
            .where(CompareJob.group_id == group_id)
        ).all()
    }

    def job_state(status: str, match) -> str:  # noqa: ANN001
        if status in ("running", "queued"):
            return "running"
        if status in ("failed", "cancelled"):
            return "fail"
        return "pass" if match is True else "diff" if match is False else "notrun"

    # Group a project's jobs into test cases (standalone id, or batch_id), collecting pair states.
    per_project: dict[str, dict[str, list[str]]] = {}
    for jid, pid, bid, status in jobs:
        if pid is None:
            continue
        case = bid or f"single:{jid}"
        per_project.setdefault(pid, {}).setdefault(case, []).append(job_state(status, match_by_job.get(jid)))

    out: dict[str, dict] = {}
    for pid, cases in per_project.items():
        h = _empty_health()
        for states in cases.values():
            if "running" in states:
                h["running"] += 1
            elif "fail" in states:
                h["failed"] += 1
            elif "diff" in states:
                h["differences"] += 1
            elif all(st == "pass" for st in states):
                h["passing"] += 1
            else:
                h["not_run"] += 1
        done = h["passing"] + h["differences"] + h["failed"]
        h["pass_rate"] = round(100.0 * h["passing"] / done, 1) if done else 0.0
        out[pid] = h
    return out


def _dict(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "tags": p.tags or [],
        "variables": p.variables or {},
        "created_by": p.created_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "archived": p.archived_at is not None,
        "archived_at": p.archived_at.isoformat() if p.archived_at else None,
    }
