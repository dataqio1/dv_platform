"""Alembic environment. Targets the ORM metadata and reads the DB URL from
DVP_DATABASE_URL, so `alembic revision --autogenerate` and `alembic upgrade head`
work in server/prod. Local mode creates the schema in-memory at boot instead.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from dvp.config import get_settings
from dvp.db.base import Base

# Ensure all models are imported so metadata is complete.
from dvp.db import models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_settings = get_settings()
_db_url = _settings.database_url or "postgresql+psycopg://dvp:dvp@localhost:5432/dvp"
config.set_main_option("sqlalchemy.url", _db_url)


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url, target_metadata=target_metadata, literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
