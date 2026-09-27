from pathlib import Path

from sqlalchemy import pool

from alembic import context
from app import models  # noqa: F401  (registers tables on Base.metadata)
from app.config import get_settings
from app.db import Base, make_engine

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    # Tests (and callers) may pin a URL on the Alembic config; otherwise use settings.
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _ensure_sqlite_dir(url: str) -> None:
    prefix = "sqlite:///"
    if url.startswith(prefix) and ":memory:" not in url:
        Path(url[len(prefix) :]).parent.mkdir(parents=True, exist_ok=True)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _database_url()
    _ensure_sqlite_dir(url)
    # Pass NullPool at creation: swapping engine.pool afterwards drops the pool's event
    # listeners (dialect init and the foreign-keys PRAGMA), which breaks SQLite reflection.
    engine = make_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
