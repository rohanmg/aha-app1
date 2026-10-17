from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401  (registers all tables)
from app.db import Base


def _database_url() -> str:
    from app.db import default_database_url

    return os.environ.get("DATABASE_URL") or default_database_url()


config = context.config
config.set_main_option("sqlalchemy.url", _database_url())
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
