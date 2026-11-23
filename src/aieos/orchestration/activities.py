"""Activities of the agent worker (task queue `aieos-agents`)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from temporalio import activity
from temporalio.exceptions import ApplicationError

from aieos.agents.errors import AgentStepError
from aieos.core.ids import uuid7
from aieos.core.logging import get_logger
from aieos.orchestration.models import (
    AgentStepRequest,
    AgentStepResult,
    GateEvent,
    PlanEvent,
    ProgressEnvelope,
    StepEvent,
    TaskEvent,
    WorkflowStatusEvent,
)
from aieos.persistence import repositories as repo
from aieos.persistence.db import Database
from aieos.providers.metering import BudgetExceededError

log = get_logger(__name__)

TERMINAL_WORKFLOW = ("succeeded", "failed", "cancelled")


class AgentRunner(Protocol):
    """Runs one agent step. The real runner (agent factory + ModelRouter + gateway tools) arrives in B14/B15."""

    async def run(self, request: AgentStepRequest, *, agent_run_id: UUID) -> AgentStepResult: ...


class NotConfiguredAgentRunner:
    async def run(self, request: AgentStepRequest, *, agent_run_id: UUID) -> AgentStepResult:
        raise ApplicationError(
            f"no runner configured for agent {request.agent_key!r} (delivered in B14/B15)",
            type="AgentNotAvailable",
            non_retryable=True,
        )


class OrchestrationActivities:
    def __init__(self, db: Database, runner: AgentRunner) -> None:
        self._db = db
        self._runner = runner

    # ------------------------------------------------------------------ progress
    @activity.defn(name="record_progress")
    async def record_progress(self, envelope: ProgressEnvelope) -> None:
        """Idempotent writes: replaying an event leaves the same state."""
        e = envelope.event
        async with self._db.tenant(e.org_id) as conn:
            if isinstance(e, WorkflowStatusEvent):
                await conn.execute(
                    text(
                        """UPDATE workflows SET status = :s, updated_at = now(),
                              finished_at = CASE WHEN :s IN ('succeeded','failed','cancelled') THEN now()
                                                 ELSE finished_at END
                       WHERE id = :wf AND status NOT IN ('succeeded','failed','cancelled')"""
                    ),
                    {"s": e.status, "wf": e.workflow_id},
                )
                if e.status in TERMINAL_WORKFLOW:
                    await repo.record_audit(
                        conn,
                        org_id=e.org_id,
                        actor_type="system",
                        actor_id="orchestrator",
                        action=f"workflow.{e.status}",
                        target_type="workflow",
                        target_id=str(e.workflow_id),
                        payload={"reason": e.reason},
                    )
            elif isinstance(e, StepEvent):
                await conn.execute(
                    text(
                        """INSERT INTO workflow_steps (id, org_id, workflow_id, step_key, sequence, attempt, status,
                                                   error, started_at, finished_at)
                       VALUES (:id, :org, :wf, :key, :seq, :attempt, :s, CAST(:err AS jsonb), now(),
                               CASE WHEN :s IN ('succeeded','failed','cancelled') THEN now() END)
                       ON CONFLICT (workflow_id, step_key, attempt) DO UPDATE
                         SET status = EXCLUDED.status, error = EXCLUDED.error,
                             finished_at = EXCLUDED.finished_at"""
                    ),
                    {
                        "id": uuid7(),
                        "org": e.org_id,
                        "wf": e.workflow_id,
                        "key": e.step_key,
                        "seq": e.sequence,
                        "attempt": e.attempt,
                        "s": e.status,
                        "err": json.dumps({"message": e.error}) if e.error else None,
                    },
                )
            elif isinstance(e, PlanEvent):
                for t in e.tasks:
                    await conn.execute(
                        text(
                            """INSERT INTO tasks (id, org_id, workflow_id, key, title, description, assigned_agent_key)
                           VALUES (:id, :org, :wf, :key, :title, :desc, 'developer')
                           ON CONFLICT (workflow_id, key) DO NOTHING"""
                        ),
                        {
                            "id": uuid7(),
                            "org": e.org_id,
                            "wf": e.workflow_id,
                            "key": t.key,
                            "title": t.title,
                            "desc": t.description,
                        },
                    )
                for t in e.tasks:
                    for dep in t.depends_on:
                        await conn.execute(
                            text(
                                """INSERT INTO task_dependencies (org_id, task_id, depends_on_task_id)
                               SELECT :org, a.id, b.id FROM tasks a, tasks b
                               WHERE a.workflow_id = :wf AND a.key = :task AND b.workflow_id = :wf AND b.key = :dep
                               ON CONFLICT DO NOTHING"""
                            ),
                            {"org": e.org_id, "wf": e.workflow_id, "task": t.key, "dep": dep},
                        )
            elif isinstance(e, TaskEvent):
                await conn.execute(
                    text(
                        """UPDATE tasks SET status = :s, attempt = :attempt, version = version + 1, updated_at = now()
                       WHERE workflow_id = :wf AND key = :key"""
                    ),
                    {"s": e.status, "attempt": e.attempt, "wf": e.workflow_id, "key": e.task_key},
                )
            elif isinstance(e, GateEvent):
                await conn.execute(
                    text(
                        """INSERT INTO quality_gates (id, org_id, workflow_id, task_id, gate_key, result, details)
                       VALUES (:id, :org, :wf,
                               (SELECT id FROM tasks WHERE workflow_id = :wf AND key = :task),
                               :gate, :result, CAST(:details AS jsonb))"""
                    ),
                    {
                        "id": uuid7(),
                        "org": e.org_id,
                        "wf": e.workflow_id,
                        "task": e.task_key,
                        "gate": e.gate_key,
                        "result": e.result,
                        "details": json.dumps(e.details, default=str),
                    },
                )

    # ------------------------------------------------------------------ agents
    async def _create_run(
        self, org_id: UUID, workflow_id: UUID, agent_key: str, step_key: str | None = None, attempt: int | None = None
    ) -> UUID:
        """One agent run per (workflow, step, attempt). A retried activity reuses it, so the runner resumes
        from its checkpoint, in the same sandbox, with the same tool-call ids."""
        async with self._db.tenant(org_id) as conn:
            if step_key is not None:
                existing = (
                    await conn.execute(
                        text(
                            """SELECT id FROM agent_runs
                               WHERE workflow_id = :wf AND step_key = :step AND step_attempt = :attempt"""
                        ),
                        {"wf": workflow_id, "step": step_key, "attempt": attempt},
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    await conn.execute(
                        text(
                            """UPDATE agent_runs SET status = 'running', finished_at = NULL, error = NULL
                               WHERE id = :id"""
                        ),
                        {"id": existing},
                    )
                    return UUID(str(existing))
            version = (
                await conn.execute(
                    text(
                        """SELECT v.id, v.contract FROM agent_versions v JOIN agents a ON a.id = v.agent_id
                   WHERE a.key = :key AND v.status = 'active' AND (a.org_id = :org OR a.org_id IS NULL)
                   ORDER BY a.org_id NULLS LAST, v.created_at DESC LIMIT 1"""
                    ),
                    {"key": agent_key, "org": org_id},
                )
            ).first()
            if version is None:
                raise ApplicationError(
                    f"agent {agent_key!r} is not published", type="ContractViolation", non_retryable=True
                )
            primary = version.contract["model"]["primary"]
            run_id = uuid7()
            await conn.execute(
                text(
                    """INSERT INTO agent_runs (id, org_id, workflow_id, agent_version_id, model_provider, model_name,
                                           status, step_key, step_attempt)
                   VALUES (:id, :org, :wf, :v, :provider, :model, 'running', :step, :attempt)"""
                ),
                {
                    "id": run_id,
                    "org": org_id,
                    "wf": workflow_id,
                    "v": version.id,
                    "provider": primary["provider"],
                    "model": primary["model"],
                    "step": step_key,
                    "attempt": attempt if step_key is not None else None,
                },
            )
        return run_id

    async def _finish_run(self, org_id: UUID, run_id: UUID, status: str, error: str | None = None) -> None:
        async with self._db.tenant(org_id) as conn:
            await conn.execute(
                text(
                    """UPDATE agent_runs SET status = :s, finished_at = now(), error = CAST(:err AS jsonb)
                   WHERE id = :id"""
                ),
                {"s": status, "id": run_id, "err": json.dumps({"message": error}) if error else None},
            )

    @activity.defn(name="run_agent_step")
    async def run_agent_step(self, request: AgentStepRequest) -> AgentStepResult:
        run_id = await self._create_run(
            request.org_id, request.workflow_id, request.agent_key, request.step_key, request.attempt
        )
        try:
            result = await self._runner.run(request, agent_run_id=run_id)
        except BudgetExceededError as exc:
            await self._finish_run(request.org_id, run_id, "budget_exceeded", str(exc))
            raise ApplicationError(str(exc), type="BudgetExceeded", non_retryable=True) from None
        except AgentStepError as exc:
            await self._finish_run(request.org_id, run_id, "failed", f"{exc.error_type}: {exc}")
            raise ApplicationError(str(exc), type=exc.error_type, non_retryable=True) from None
        except Exception as exc:
            await self._finish_run(request.org_id, run_id, "failed", type(exc).__name__)
            raise
        await self._finish_run(request.org_id, run_id, "succeeded")
        return result.model_copy(update={"agent_run_id": run_id})

    @activity.defn(name="ensure_supervisor_run")
    async def ensure_supervisor_run(self, org_id: UUID, workflow_id: UUID) -> UUID:
        """One supervisor run per workflow; it is the actor for orchestrator-level tool calls (PR, merge)."""
        async with self._db.tenant(org_id) as conn:
            existing = (
                await conn.execute(
                    text(
                        """SELECT r.id FROM agent_runs r JOIN agent_versions v ON v.id = r.agent_version_id
                   JOIN agents a ON a.id = v.agent_id
                   WHERE r.workflow_id = :wf AND a.key = 'supervisor' ORDER BY r.started_at LIMIT 1"""
                    ),
                    {"wf": workflow_id},
                )
            ).first()
        if existing is not None:
            return UUID(str(existing.id))
        return await self._create_run(org_id, workflow_id, "supervisor")

    # ------------------------------------------------------------------ approvals and cleanup
    @activity.defn(name="get_approval_status")
    async def get_approval_status(self, org_id: UUID, approval_id: UUID) -> str:
        async with self._db.tenant(org_id) as conn:
            row = await conn.execute(
                text(
                    """UPDATE approvals SET status = 'expired', version = version + 1
                   WHERE id = :id AND status = 'pending' AND expires_at <= now()"""
                ),
                {"id": approval_id},
            )
            del row
            status = (
                await conn.execute(text("SELECT status FROM approvals WHERE id = :id"), {"id": approval_id})
            ).scalar()
        return str(status or "missing")

    @activity.defn(name="cleanup_workflow")
    async def cleanup_workflow(self, org_id: UUID, workflow_id: UUID) -> None:
        """Releases workflow resources. Sandboxes join this in B13 (stop every task tagged with the workflow id)."""
        async with self._db.tenant(org_id) as conn:
            await conn.execute(
                text(
                    """UPDATE agent_runs SET status = 'cancelled', finished_at = now()
                   WHERE workflow_id = :wf AND status = 'running'"""
                ),
                {"wf": workflow_id},
            )
            await conn.execute(
                text(
                    """UPDATE approvals SET status = 'cancelled', version = version + 1
                   WHERE workflow_id = :wf AND status = 'pending'"""
                ),
                {"wf": workflow_id},
            )
        log.info("workflow.cleanup", workflow_id=str(workflow_id))

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.record_progress,
            self.run_agent_step,
            self.ensure_supervisor_run,
            self.get_approval_status,
            self.cleanup_workflow,
        ]
