"""Run a multi-statement SQL script on the migration connection.

Uses the raw psycopg connection with no parameters, so ``%`` in DO blocks and functions is
not read as a placeholder.
"""

from typing import Any

from alembic import op


def run_sql(sql: str) -> None:
    driver_connection: Any = op.get_bind().connection.driver_connection
    driver_connection.execute(sql)
