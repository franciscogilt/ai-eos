"""Tool gateway results and approval de-duplication.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-21
"""

from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0003_gateway_and_approvals.sql"


def upgrade() -> None:
    dbapi_conn = op.get_bind().connection.dbapi_connection
    with dbapi_conn.cursor() as cur:
        cur.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("Forward-only migrations: write a new expand/contract migration instead.")
