"""Alembic environment. Migrations are hand-written SQL (RLS policies, grants), so there is
no autogenerate metadata. They run as the schema owner (``CLOUDOPTICS_DATABASE_URL``)."""

from alembic import context
from sqlalchemy import create_engine, pool

from cloudoptics.platform.settings import get_settings

url = context.config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_online() -> None:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
