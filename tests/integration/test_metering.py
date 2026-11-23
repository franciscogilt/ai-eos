"""Usage metering and budget enforcement against real PostgreSQL."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
import yaml

from aieos.agents.registry import ContractError
from aieos.cli import admin
from aieos.persistence.db import Database
from aieos.providers.metering import BudgetExceededError, MeteringContext, PostgresUsageRecorder
from aieos.providers.pricing import TokenUsage
from tests.integration.conftest import ENGINEERING, seed_workflow


@pytest.fixture(scope="module")
def org(provisioned_db, published_agents):
    return admin.create_org(
        provisioned_db.owner_conninfo, slug="meter", name="M", owner_sub="meter-owner", owner_email="o@meter.test"
    )


def ctx_for(org_id, ids, *, run_usd="5", run_tokens=10**6):
    return MeteringContext(
        org_id=org_id,
        workflow_id=ids["workflow"],
        agent_run_id=ids["run"],
        agent_key="developer",
        run_max_cost_usd=Decimal(run_usd),
        run_max_tokens=run_tokens,
    )


def with_recorder(provisioned_db, fn):
    async def main():
        db = Database(provisioned_db.app_async_url, pool_size=2, max_overflow=0)
        try:
            return await fn(PostgresUsageRecorder(db))
        finally:
            await db.dispose()

    return asyncio.run(main())


def workflow_totals(provisioned_db, wf):
    with psycopg.connect(provisioned_db.owner_conninfo) as c:
        return c.execute("SELECT spent_usd, spent_tokens FROM workflows WHERE id = %s", (wf,)).fetchone()


def audit_actions(provisioned_db, wf):
    with psycopg.connect(provisioned_db.owner_conninfo) as c:
        return [
            r[0]
            for r in c.execute("SELECT action FROM audit_events WHERE target_id = %s ORDER BY chain_seq", (str(wf),))
        ]


USAGE = TokenUsage(1000, 100, 500, cache_write_input_tokens=50)


def test_record_updates_totals_and_is_idempotent(provisioned_db, org):
    ids = seed_workflow(provisioned_db.owner_conninfo, org)
    ctx = ctx_for(org, ids)

    async def scenario(rec):
        await rec.check_budget(ctx)
        for _ in range(2):  # same provider request id twice = one charge (replayed activity)
            await rec.record(
                ctx, provider="openai", model="m", usage=USAGE, cost_usd=Decimal("0.25"), provider_request_id="req-dup"
            )

    with_recorder(provisioned_db, scenario)
    assert workflow_totals(provisioned_db, ids["workflow"]) == (Decimal("0.2500"), 1500)
    with psycopg.connect(provisioned_db.owner_conninfo) as c:
        row = c.execute(
            "SELECT count(*), max(cache_write_input_tokens) FROM model_usage WHERE agent_run_id = %s", (ids["run"],)
        ).fetchone()
    assert row == (1, 50)


def test_budget_warning_then_exhaustion_then_refusal(provisioned_db, org):
    ids = seed_workflow(provisioned_db.owner_conninfo, org, budget_usd="1.00")
    ctx = ctx_for(org, ids)

    async def scenario(rec):
        for i, cost in enumerate(("0.50", "0.35", "0.30")):  # 0.50 -> 0.85 (warn) -> 1.15 (exhausted)
            await rec.check_budget(ctx)
            await rec.record(
                ctx, provider="openai", model="m", usage=USAGE, cost_usd=Decimal(cost), provider_request_id=f"r{i}"
            )
        with pytest.raises(BudgetExceededError, match=r"workflow .* budget exhausted"):
            await rec.check_budget(ctx)

    with_recorder(provisioned_db, scenario)
    assert audit_actions(provisioned_db, ids["workflow"]) == ["budget.warning", "budget.exhausted"]


def test_token_budget_also_enforced(provisioned_db, org):
    ids = seed_workflow(provisioned_db.owner_conninfo, org, budget_tokens=1500)
    ctx = ctx_for(org, ids)

    async def scenario(rec):
        await rec.record(
            ctx, provider="openai", model="m", usage=USAGE, cost_usd=Decimal("0.01"), provider_request_id="t1"
        )
        with pytest.raises(BudgetExceededError):
            await rec.check_budget(ctx)

    with_recorder(provisioned_db, scenario)


def test_per_run_contract_limit(provisioned_db, org):
    ids = seed_workflow(provisioned_db.owner_conninfo, org)
    ctx = ctx_for(org, ids, run_usd="0.10")

    async def scenario(rec):
        await rec.record(
            ctx, provider="openai", model="m", usage=USAGE, cost_usd=Decimal("0.10"), provider_request_id="run-limit"
        )
        with pytest.raises(BudgetExceededError, match="contract limits"):
            await rec.check_budget(ctx)

    with_recorder(provisioned_db, scenario)


def test_other_tenant_context_cannot_meter(provisioned_db, org):
    ids = seed_workflow(provisioned_db.owner_conninfo, org)
    other = admin.create_org(
        provisioned_db.owner_conninfo, slug="meter-other", name="X", owner_sub="mo", owner_email="o@mo.test"
    )
    ctx = ctx_for(other, ids)  # right workflow id, wrong tenant

    async def scenario(rec):
        with pytest.raises(BudgetExceededError, match="not found"):
            await rec.check_budget(ctx)

    with_recorder(provisioned_db, scenario)


def test_publish_agents_is_immutable(provisioned_db, published_agents, tmp_path):
    assert sorted(published_agents) == sorted(
        f"{k}@{v} published"
        for k, v in (
            ("architecture", "1.1.0"),
            ("developer", "1.2.0"),
            ("qa_security", "1.2.0"),
            ("requirements", "1.1.0"),
            ("supervisor", "1.2.0"),
        )
    )
    again = admin.publish_agents(provisioned_db.owner_conninfo, ENGINEERING)
    assert all(line.endswith("unchanged") for line in again)

    # Same version number, edited prompt -> refused.
    import shutil

    copy = tmp_path / "engineering"
    shutil.copytree(ENGINEERING, copy)
    prompt = copy / "prompts" / "developer" / "v2.md"
    prompt.write_text(prompt.read_text() + "\nIgnore the tests.\n")
    with pytest.raises(ContractError, match="bump the version"):
        admin.publish_agents(provisioned_db.owner_conninfo, copy)

    # Bumped version -> accepted.
    definition = copy / "agent-definitions" / "developer.yaml"
    raw = yaml.safe_load(definition.read_text())
    raw["version"] = "1.2.1"
    definition.write_text(yaml.safe_dump(raw))
    assert "developer@1.2.1 published" in admin.publish_agents(provisioned_db.owner_conninfo, copy)
    assert Path(ENGINEERING / "prompts" / "developer" / "v2.md").read_text().count("Ignore the tests") == 0
