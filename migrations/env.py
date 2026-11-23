"""Alembic environment. Migrations are reviewed SQL files executed verbatim.

Runs as the table-owner role (OWNER_DATABASE_URL). The runtime role never migrates.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool


def _url() -> str:
    from aieos.core.dburl import database_url

    url = database_url("OWNER_DATABASE_URL", "OWNER_DB")
    if not url:
        raise RuntimeError("OWNER_DATABASE_URL (or OWNER_DB_HOST/NAME/USER/PASSWORD) is not set")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
