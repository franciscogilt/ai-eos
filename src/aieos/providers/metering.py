"""Usage metering and budget enforcement.

Checked before every model call; recorded after every successful call.
Two budgets apply: the workflow's (workflows.budget_*) and the agent run's
(contract limits). Recording is idempotent per provider request id, so a
replayed Temporal activity cannot charge twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import text

from aieos.core.ids import uuid7
from aieos.core.logging import get_logger
from aieos.persistence import repositories as repo
from aieos.persistence.db import Database
from aieos.providers.pricing import TokenUsage

log = get_logger(__name__)

WARNING_FRACTION = Decimal("0.8")


class BudgetExceededError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MeteringContext:
    org_id: UUID
    workflow_id: UUID
    agent_run_id: UUID
    agent_key: str
    run_max_cost_usd: Decimal
    run_max_tokens: int


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    workflow_spent_usd: Decimal
    workflow_budget_usd: Decimal
    workflow_spent_tokens: int
    workflow_budget_tokens: int
    run_spent_usd: Decimal
    run_spent_tokens: int


class UsageRecorder(Protocol):
    async def check_budget(self, ctx: MeteringContext) -> BudgetStatus: ...

    async def record(
        self,
        ctx: MeteringContext,
        *,
        provider: str,
        model: str,
        usage: TokenUsage,
        cost_usd: Decimal,
        provider_request_id: str | None,
    ) -> None: ...


def _crossed(before: Decimal, after: Decimal, limit: Decimal) -> bool:
    return before < limit <= after


class PostgresUsageRecorder:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def check_budget(self, ctx: MeteringContext) -> BudgetStatus:
        async with self._db.tenant(ctx.org_id) as conn:
            row = (
                await conn.execute(
                    text(
                        """SELECT w.spent_usd, w.budget_usd, w.spent_tokens, w.budget_tokens,
                                  coalesce(sum(u.cost_usd), 0) AS run_usd,
                                  coalesce(sum(u.input_tokens + u.output_tokens), 0) AS run_tokens
                           FROM workflows w
                           LEFT JOIN model_usage u ON u.agent_run_id = :run
                           WHERE w.id = :wf
                           GROUP BY w.id"""
                    ),
                    {"wf": ctx.workflow_id, "run": ctx.agent_run_id},
                )
            ).first()
        if row is None:
            raise BudgetExceededError(f"workflow {ctx.workflow_id} not found; refusing unmetered model call")
        status = BudgetStatus(
            Decimal(row.spent_usd),
            Decimal(row.budget_usd),
            int(row.spent_tokens),
            int(row.budget_tokens),
            Decimal(row.run_usd),
            int(row.run_tokens),
        )
        if status.workflow_spent_usd >= status.workflow_budget_usd or (
            status.workflow_spent_tokens >= status.workflow_budget_tokens
        ):
            raise BudgetExceededError(f"workflow {ctx.workflow_id} budget exhausted")
        if status.run_spent_usd >= ctx.run_max_cost_usd or status.run_spent_tokens >= ctx.run_max_tokens:
            raise BudgetExceededError(f"agent run {ctx.agent_run_id} ({ctx.agent_key}) exceeded its contract limits")
        return status

    async def record(
        self,
        ctx: MeteringContext,
        *,
        provider: str,
        model: str,
        usage: TokenUsage,
        cost_usd: Decimal,
        provider_request_id: str | None,
    ) -> None:
        async with self._db.tenant(ctx.org_id) as conn:
            inserted = (
                await conn.execute(
                    text(
                        """INSERT INTO model_usage (id, org_id, workflow_id, agent_run_id, provider, model,
                                                    provider_request_id, input_tokens, cached_input_tokens,
                                                    cache_write_input_tokens, output_tokens, cost_usd)
                           VALUES (:id, :org, :wf, :run, :provider, :model, :req, :inp, :cached, :cwrite, :out,
                                   :cost)
                           ON CONFLICT (org_id, provider, provider_request_id) DO NOTHING
                           RETURNING id"""
                    ),
                    {
                        "id": uuid7(),
                        "org": ctx.org_id,
                        "wf": ctx.workflow_id,
                        "run": ctx.agent_run_id,
                        "provider": provider,
                        "model": model,
                        "req": provider_request_id,
                        "inp": usage.input_tokens,
                        "cached": usage.cached_input_tokens,
                        "cwrite": usage.cache_write_input_tokens,
                        "out": usage.output_tokens,
                        "cost": cost_usd,
                    },
                )
            ).first()
            if inserted is None:
                log.info("model_usage.duplicate_ignored", provider=provider, request_id=provider_request_id)
                return
            totals = (
                await conn.execute(
                    text(
                        """UPDATE workflows
                           SET spent_usd = spent_usd + :cost, spent_tokens = spent_tokens + :tokens,
                               updated_at = now()
                           WHERE id = :wf
                           RETURNING spent_usd, budget_usd, spent_tokens, budget_tokens"""
                    ),
                    {"cost": cost_usd, "tokens": usage.total_tokens, "wf": ctx.workflow_id},
                )
            ).one()
            after_usd, budget_usd = Decimal(totals.spent_usd), Decimal(totals.budget_usd)
            after_tok, budget_tok = Decimal(totals.spent_tokens), Decimal(totals.budget_tokens)
            before_usd, before_tok = after_usd - cost_usd, after_tok - usage.total_tokens
            for action, fraction in (("budget.warning", WARNING_FRACTION), ("budget.exhausted", Decimal(1))):
                if _crossed(before_usd, after_usd, budget_usd * fraction) or _crossed(
                    before_tok, after_tok, budget_tok * fraction
                ):
                    await repo.record_audit(
                        conn,
                        org_id=ctx.org_id,
                        actor_type="system",
                        actor_id="model-router",
                        action=action,
                        target_type="workflow",
                        target_id=str(ctx.workflow_id),
                        payload={
                            "spent_usd": str(after_usd),
                            "budget_usd": str(budget_usd),
                            "spent_tokens": int(after_tok),
                            "budget_tokens": int(budget_tok),
                        },
                    )
                    log.warning(action, workflow_id=str(ctx.workflow_id), spent_usd=str(after_usd))
