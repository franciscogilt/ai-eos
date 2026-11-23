"""Base commit of a workflow (B13): the snapshot every sandbox and push uses.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-22
"""

from pathlib import Path

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / "0005_workflow_base_commit.sql"


def upgrade() -> None:
    dbapi_conn = op.get_bind().connection.dbapi_connection
    with dbapi_conn.cursor() as cur:
        cur.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    raise NotImplementedError("Forward-only migrations: write a new expand/contract migration instead.")
