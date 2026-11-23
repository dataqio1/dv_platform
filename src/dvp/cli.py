"""`dvp` command-line entrypoint.

dvp run       # boot API (+ in-memory DB in local mode), zero external deps
dvp worker    # run a worker that executes queued compares as separate processes
dvp version   # print version
dvp migrate   # apply metadata migrations (server/prod)
"""

from __future__ import annotations

import typer

from dvp import __version__
from dvp.config import get_settings

app = typer.Typer(help="DV Platform CLI", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the platform version."""
    typer.echo(f"dv-platform {__version__}")


@app.command()
def worker() -> None:
    """Run a worker that executes queued compares as separate OS processes.

    The metadata DB is the queue (no Redis). Each claimed compare runs as its own
    `dvp.workers.run_compare` process with its own Spark session (`DVP_SPARK_MASTER`).
    Run one or more of these alongside `dvp run`; scale out by starting more.
    """
    from dvp.abstractions.factory import build_container
    from dvp.workers.worker import Worker

    settings = get_settings()
    container = build_container(settings)
    if settings.is_local:
        container.metadata.create_all()
    typer.echo(
        f"Starting DV Platform worker [{settings.profile}] — "
        f"spark={settings.spark_master}, max_concurrent={settings.max_concurrent_jobs}"
    )
    Worker(container).run_forever()


@app.command()
def run(
    host: str | None = typer.Option(None, help="Bind host (default from settings)"),
    port: int | None = typer.Option(None, help="Bind port (default from settings)"),
) -> None:
    """Run the API server. In local mode this uses an in-memory SQLite DB."""
    import uvicorn

    settings = get_settings()
    # uvloop can segfault under load on some macOS/Python builds; prefer the stable
    # asyncio/h11 stack in local mode unless the operator overrides it explicitly.
    loop = settings.uvicorn_loop
    http = settings.uvicorn_http
    if settings.is_local:
        if loop == "auto":
            loop = "asyncio"
        if http == "auto":
            http = "h11"
    typer.echo(
        f"Starting DV Platform [{settings.profile}] — docs at /docs (loop={loop}, http={http})"
    )
    uvicorn.run(
        "dvp.api.app:create_app",
        factory=True,
        host=host or settings.api_host,
        port=port or settings.api_port,
        log_level=settings.log_level.lower(),
        loop=loop,
        http=http,
    )


@app.command()
def migrate() -> None:
    """Apply Alembic migrations (server/prod). In local mode the schema is created
    automatically at boot, so this is a no-op."""
    settings = get_settings()
    if settings.is_local:
        typer.echo("local mode: schema is created in-memory at boot; nothing to migrate.")
        raise typer.Exit(0)
    typer.echo("Run: alembic upgrade head  (see migrations/)")


if __name__ == "__main__":
    app()
