"""FastAPI application factory.

On startup (local mode) it creates the schema in the in-memory SQLite DB and
auto-provisions the single org/group/user, then wires the no-password identity
provider — so `dvp run` boots with zero external dependencies.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders

from dvp.abstractions.factory import build_container, build_identity_provider
from dvp.config import Settings, get_settings
from dvp.core.errors import DVPError
from dvp.core.logging import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
)
from dvp.db.base import new_id
from dvp.services.tenancy import bootstrap_local_tenant

API_PREFIX = "/api/v1"
REQUEST_ID_HEADER = "X-Request-ID"
log = get_logger("dvp.api")


class CorrelationIdMiddleware:
    """Pure-ASGI correlation id. Honors a caller-supplied X-Request-ID (so a trace survives across
    services) else mints one; binds it into the structlog context so every log line of the request
    is tagged, stashes it on scope state so the unhandled-error handler can quote the same id, and
    echoes it back on the response header.

    Deliberately NOT a BaseHTTPMiddleware: that variant tears down its context before Starlette's
    outer ServerErrorMiddleware runs the `Exception` handler, which would break the 500 envelope.
    """

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001, ANN204
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or {}).get(b"x-request-id")
        request_id = incoming.decode("latin-1") if incoming else new_id()[:12]
        scope.setdefault("state", {})["request_id"] = request_id
        bind_request_context(request_id=request_id)

        async def send_wrapper(message):  # noqa: ANN001, ANN202
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            clear_request_context()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        container = build_container(settings)
        scheduler = None
        if settings.is_local:
            container.metadata.create_all()
            local = bootstrap_local_tenant(container.metadata, settings)
            app.state.local_tenant = local
            container.identity = build_identity_provider(
                settings, local_user_id=local.user_id, local_org_id=local.org_id
            )
            log.info("local_boot_complete", org=local.org_id, group=local.group_id)
            # A restart strands any in-flight run as a phantom "running" job (the in-memory worker
            # set is gone). Sweep them to failed so Executions/health don't show perpetual spinners.
            # The thread executor also loses queued work on restart, so reap those too; a durable
            # (subprocess) queue keeps its queued jobs for a worker to pick up — leave those.
            try:
                from dvp.testing_engine.service import reap_orphaned_runs

                reaped = reap_orphaned_runs(
                    container.metadata, include_queued=(settings.compare_executor == "thread")
                )
                if reaped:
                    log.info("reaped_orphaned_runs", count=reaped)
            except Exception:  # noqa: BLE001 — never block boot on cleanup
                log.warning("reap_orphaned_runs_failed", exc_info=True)
            # Re-apply the admin's saved worker-pool limits (memory / vCPUs) to the live scheduler.
            try:
                from dvp.services.org_settings import OrgSettingsService
                from dvp.services.tenancy import resolve_context

                _ctx0 = resolve_context(
                    container.metadata, user_id=local.user_id, org_id=local.org_id, group_id=local.group_id
                )
                _comp = (OrgSettingsService(container.metadata).get(_ctx0, "compute") or {}).get("value") or {}
                if hasattr(container.jobs, "set_limits") and (_comp.get("total_memory_gb") or _comp.get("max_vcpus")):
                    container.jobs.set_limits(
                        total_memory_gb=float(_comp["total_memory_gb"]) if _comp.get("total_memory_gb") else None,
                        max_vcpus=int(_comp["max_vcpus"]) if _comp.get("max_vcpus") else None,
                    )
            except Exception:  # noqa: BLE001 — never block boot on settings
                pass
            if settings.scheduler_enabled:
                from dvp.observability.scheduler import Scheduler
                from dvp.services.tenancy import resolve_context

                ctx = resolve_context(
                    container.metadata,
                    user_id=local.user_id,
                    org_id=local.org_id,
                    group_id=local.group_id,
                )
                scheduler = Scheduler(container, settings, ctx)
                scheduler.start()
        else:
            # server/prod: schema via Alembic; identity via OIDC (configured elsewhere).
            container.identity = build_identity_provider(
                settings, local_user_id="", local_org_id=""
            )
        app.state.container = container
        # Register the notification dispatcher so new incidents alert configured channels.
        from dvp.integrations.dispatch import NotificationDispatcher, set_dispatcher

        set_dispatcher(NotificationDispatcher(container))

        # Desktop convenience: embed a compare worker so the out-of-process executor works
        # from a single `dvp run`. Server/prod run `dvp worker` as its own process(es).
        embedded_worker = None
        worker_thread = None
        if settings.compare_executor == "subprocess" and settings.embed_worker:
            import threading

            from dvp.workers.worker import Worker

            embedded_worker = Worker(container, worker_id="embedded")
            worker_thread = threading.Thread(
                target=embedded_worker.run_forever, name="dvp-embedded-worker", daemon=True
            )
            worker_thread.start()
            log.info("embedded_worker_started", spark=settings.spark_master)

        yield
        if embedded_worker is not None:
            embedded_worker.stop()
        set_dispatcher(None)
        if scheduler is not None:
            scheduler.stop()

    app = FastAPI(title="DV Platform", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CorrelationIdMiddleware)

    @app.exception_handler(DVPError)
    async def _dvp_error_handler(_request: Request, exc: DVPError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "type": f"about:blank#{exc.code}",
                "title": exc.code,
                "status": exc.http_status,
                "detail": exc.message,
                "errors": exc.details,
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all so an unexpected failure still returns the problem+json envelope instead of a
        raw stack trace (golden rule #3 — never fail silently, never leak internals). The full
        exception is logged with a short error_id the client can quote to support; the response body
        carries only that id, never the message or trace. The id IS the request's correlation id
        (also in the X-Request-ID header and every log line), so support can jump straight to it."""
        error_id = getattr(request.state, "request_id", None) or new_id()[:12]
        log.error(
            "unhandled_exception",
            error_id=error_id,
            path=request.url.path,
            method=request.method,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content={
                "type": "about:blank#internal_error",
                "title": "internal_error",
                "status": 500,
                "detail": "An unexpected error occurred. Quote the error_id when contacting support.",
                "errors": {"error_id": error_id},
            },
            # ServerErrorMiddleware emits this response outside the correlation middleware, so set
            # the header here too — the 500 still carries X-Request-ID == error_id.
            headers={REQUEST_ID_HEADER: error_id},
        )

    from dvp.api.routers import (
        access,
        audit,
        compares,
        connections,
        dbt,
        feedback,
        finder,
        health,
        integrations,
        lineage,
        me,
        members,
        metadata_conventions,
        metrics,
        monitors,
        observability,
        projects,
        reports,
        system,
        table_metadata,
        tags,
    )
    from dvp.api.routers import settings as sample_settings

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(lineage.router, prefix=API_PREFIX)
    app.include_router(audit.router, prefix=API_PREFIX)
    app.include_router(me.router, prefix=API_PREFIX)
    app.include_router(members.router, prefix=API_PREFIX)
    app.include_router(access.router, prefix=API_PREFIX)
    app.include_router(system.router, prefix=API_PREFIX)
    app.include_router(integrations.router, prefix=API_PREFIX)
    app.include_router(metrics.router, prefix=API_PREFIX)
    app.include_router(connections.router, prefix=API_PREFIX)
    app.include_router(finder.router, prefix=API_PREFIX)
    app.include_router(dbt.router, prefix=API_PREFIX)
    app.include_router(monitors.router, prefix=API_PREFIX)
    app.include_router(observability.router, prefix=API_PREFIX)
    app.include_router(compares.router, prefix=API_PREFIX)
    app.include_router(projects.router, prefix=API_PREFIX)
    app.include_router(tags.router, prefix=API_PREFIX)
    app.include_router(table_metadata.router, prefix=API_PREFIX)
    app.include_router(metadata_conventions.router, prefix=API_PREFIX)
    app.include_router(feedback.router, prefix=API_PREFIX)
    app.include_router(reports.router, prefix=API_PREFIX)
    app.include_router(sample_settings.router, prefix=API_PREFIX)

    _mount_spa(app)
    return app


def _mount_spa(app: FastAPI) -> None:
    """Serve the built frontend (frontend/dist) from the API itself, so `dvp run` is the ONLY
    server needed — the SPA is same-origin with /api (no Vite, no proxy, no CORS). No-op if the
    bundle hasn't been built. API/docs routes are registered first and always win; everything
    else falls through to index.html so client-side routes (e.g. /admin/access) deep-link."""
    from pathlib import Path

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    index = dist / "index.html"
    if not index.is_file():
        log.info("spa_not_built", hint="run `npm run build` in frontend/ to serve the UI from the API")
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        # Never shadow the API or the OpenAPI docs — those are explicit routes, but be defensive.
        if full_path.startswith(("api/", "docs", "redoc", "openapi.json")):
            return FileResponse(index, status_code=404)
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)  # SPA fallback (client-side routing)
