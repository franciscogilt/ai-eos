"""Initial schema: tenancy, agents, workflows, execution records, audit.

Revision ID: 0001
Revises:
Create Date: 2026-11-21
"""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0001_initial_schema.sql"


def upgrade() -> None:
    # Execute verbatim on the DBAPI cursor with no parameters, so neither SQLAlchemy (':name')
    # nor psycopg ('%s') placeholder parsing touches the SQL. Runs inside Alembic's transaction.
    dbapi_conn = op.get_bind().connection.dbapi_connection
    with dbapi_conn.cursor() as cur:
        cur.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("Forward-only migrations: write a new expand/contract migration instead.")
