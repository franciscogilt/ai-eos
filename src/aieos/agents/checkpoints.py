"""Agent-run checkpoints: the state a retried step resumes from (migration 0006).

State is JSON: the retry attempt, its feedback, the conversation so far, the tool-call counter, and,
once the step has finished, its result. Rows are tenant-scoped by RLS like every other run table.
"""

from __future__ import annotations

import json
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text

from aieos.persistence.db import Database


class Checkpoints(Protocol):
    async def load(self, org_id: UUID, agent_run_id: UUID) -> dict[str, Any] | None: ...
    async def save(self, org_id: UUID, agent_run_id: UUID, state: dict[str, Any]) -> None: ...


class PostgresCheckpoints:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self, org_id: UUID, agent_run_id: UUID) -> dict[str, Any] | None:
        async with self._db.tenant(org_id) as conn:
            value = (
                await conn.execute(
                    text("SELECT state FROM agent_run_checkpoints WHERE agent_run_id = :run"), {"run": agent_run_id}
                )
            ).scalar_one_or_none()
        return dict(value) if value is not None else None

    async def save(self, org_id: UUID, agent_run_id: UUID, state: dict[str, Any]) -> None:
        async with self._db.tenant(org_id) as conn:
            await conn.execute(
                text(
                    """INSERT INTO agent_run_checkpoints (org_id, agent_run_id, state)
                       VALUES (:org, :run, CAST(:state AS jsonb))
                       ON CONFLICT (org_id, agent_run_id)
                       DO UPDATE SET state = EXCLUDED.state, updated_at = now()"""
                ),
                {"org": org_id, "run": agent_run_id, "state": json.dumps(state, default=str)},
            )
