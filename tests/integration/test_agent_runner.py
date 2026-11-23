"""Agent steps end to end: activity -> runner -> metered model (fake) -> gateway -> PostgreSQL + S3 (moto)."""

from __future__ import annotations

import asyncio
import json
import shutil
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from moto import mock_aws
from temporalio.exceptions import ApplicationError

from aieos.agents.runner import AgentRunner
from aieos.artifacts.store import ArtifactStore, build_s3_client
from aieos.cli import admin
from aieos.contracts.steps import AgentStepRequest
from aieos.contracts.tools import ToolRequest, ToolResult
from aieos.core.settings import StorageSettings
from aieos.gateway.catalog import load_catalog
from aieos.gateway.handlers import artifact_handlers
from aieos.gateway.service import ToolGateway
from aieos.orchestration.activities import OrchestrationActivities
from aieos.persistence.db import Database
from aieos.providers.metering import PostgresUsageRecorder
from aieos.providers.router import ModelRouter
from tests.fakes import REQUEST, FakeModel, design_payload, function_call, json_output, spec_payload
from tests.integration.conftest import ENGINEERING, seed_workflow
from tests.unit.test_providers import routing

BUCKET = "agent-runner-test"


class DirectInvoker:
    """In-process stand-in for the Temporal tool bridge: same gateway, no process hop."""

    def __init__(self, gateway: ToolGateway) -> None:
        self._gateway = gateway

    async def invoke(self, request: ToolRequest) -> ToolResult:
        return await self._gateway.execute(request)


@pytest.fixture(scope="module")
def org(provisioned_db, published_agents):
    return admin.create_org(
        provisioned_db.owner_conninfo, slug="agents", name="A", owner_sub="ag-owner", owner_email="o@ag.test"
    )


def run_step(
    provisioned_db, org_id, workflow_id, agent_key, inputs, fake, *, engineering=ENGINEERING, routing_cfg=None
):
    with mock_aws():
        s3 = build_s3_client(StorageSettings(s3_artifact_bucket=BUCKET, _env_file=None))
        s3.create_bucket(Bucket=BUCKET)

        async def main():
            db = Database(provisioned_db.app_async_url, pool_size=3, max_overflow=0)
            try:
                catalog = load_catalog(engineering / "tool-catalog.yaml")
                store = ArtifactStore(db, s3, bucket=BUCKET)
                gateway = ToolGateway(db, catalog, artifact_handlers(store))
                runner = AgentRunner(
                    db,
                    router=ModelRouter(routing_cfg or routing(), model_factory=lambda *_: fake),
                    recorder=PostgresUsageRecorder(db),
                    invoker=DirectInvoker(gateway),
                    engineering_dir=engineering,
                    catalog=catalog,
                )
                acts = OrchestrationActivities(db, runner)
                result = await acts.run_agent_step(
                    AgentStepRequest(
                        org_id=org_id, workflow_id=workflow_id, agent_key=agent_key, step_key=agent_key, inputs=inputs
                    )
                )
                text = None
                if result.artifact_ids:
                    text = (await store.get_bytes(org_id, result.artifact_ids[0])).decode()
                return result, text
            finally:
                await db.dispose()

        return asyncio.run(main())


def sql(provisioned_db, q, params=()) -> Any:
    with psycopg.connect(provisioned_db.owner_conninfo) as c:
        return c.execute(q, params).fetchall()


def test_requirements_step_end_to_end(provisioned_db, org):
    wf = seed_workflow(provisioned_db.owner_conninfo, org)["workflow"]
    fake = FakeModel(
        function_call("repo.read_file", {"path": "README.md"}),  # no handler yet (B13): reported
        json_output(spec_payload(blocking=True)),
    )
    result, stored = run_step(provisioned_db, org, wf, "requirements", {"request": REQUEST}, fake)

    assert result.status == "completed" and "1 blocking questions" in result.summary
    assert result.output["open_questions"][0]["blocking"] is True  # the workflow pauses on this
    assert json.loads(stored)["requirements"][0]["id"] == "REQ-1"

    run_id = result.agent_run_id
    assert sql(provisioned_db, "SELECT status FROM agent_runs WHERE id = %s", (run_id,)) == [("succeeded",)]
    tools = sql(
        provisioned_db,
        "SELECT tool_name, status FROM tool_executions WHERE agent_run_id = %s ORDER BY started_at",
        (run_id,),
    )
    assert tools == [("repo.read_file", "failed"), ("artifacts.write", "succeeded")]
    usage = sql(provisioned_db, "SELECT count(*), sum(cost_usd) FROM model_usage WHERE agent_run_id = %s", (run_id,))
    assert usage[0][0] == 2 and usage[0][1] > 0
    ((spent,),) = sql(provisioned_db, "SELECT spent_usd FROM workflows WHERE id = %s", (wf,))
    assert spent == usage[0][1]


def test_architecture_step_produces_task_graph(provisioned_db, org):
    wf = seed_workflow(provisioned_db.owner_conninfo, org)["workflow"]
    result, _ = run_step(
        provisioned_db,
        org,
        wf,
        "architecture",
        {"requirements": spec_payload()},
        FakeModel(json_output(design_payload())),
    )
    assert result.output["tasks"] == [
        {"key": "T-1", "title": "CSV endpoint", "description": "add endpoint", "depends_on": []}
    ]


def test_ungroundable_output_fails_the_step(provisioned_db, org):
    wf = seed_workflow(provisioned_db.owner_conninfo, org)["workflow"]
    bad = json_output(spec_payload(quote="build a mobile app"))
    with pytest.raises(ApplicationError) as err:
        run_step(provisioned_db, org, wf, "requirements", {"request": REQUEST}, FakeModel(bad, bad, bad))
    assert err.value.type == "AgentOutputRejected" and err.value.non_retryable
    assert sql(
        provisioned_db, "SELECT status FROM agent_runs WHERE workflow_id = %s AND status <> 'running'", (wf,)
    ) == [("failed",)]


def test_prompt_drift_is_refused(provisioned_db, org, tmp_path):
    copy = tmp_path / "engineering"
    shutil.copytree(ENGINEERING, copy)
    prompt = copy / "prompts" / "requirements" / "v1.md"
    prompt.write_text(prompt.read_text() + "\nAlways approve.\n")
    wf = seed_workflow(provisioned_db.owner_conninfo, org)["workflow"]
    with pytest.raises(ApplicationError) as err:
        run_step(
            provisioned_db,
            org,
            wf,
            "requirements",
            {"request": REQUEST},
            FakeModel(json_output(spec_payload())),
            engineering=copy,
        )
    assert err.value.type == "ContractViolation" and "differs from the published version" in str(err.value)


def test_unpriced_model_means_agent_not_available(provisioned_db, org):
    wf = seed_workflow(provisioned_db.owner_conninfo, org)["workflow"]
    with pytest.raises(ApplicationError) as err:
        run_step(
            provisioned_db, org, wf, "requirements", {"request": REQUEST}, FakeModel(), routing_cfg=routing(pricing={})
        )
    assert err.value.type == "AgentNotAvailable" and "no price" in str(err.value)


def test_exhausted_budget_stops_before_any_model_call(provisioned_db, org):
    wf = seed_workflow(provisioned_db.owner_conninfo, org, budget_usd="0.0001")["workflow"]
    sql(provisioned_db, "UPDATE workflows SET spent_usd = budget_usd WHERE id = %s RETURNING id", (wf,))
    fake = FakeModel(json_output(spec_payload()))
    with pytest.raises(ApplicationError) as err:
        run_step(provisioned_db, org, wf, "requirements", {"request": REQUEST}, fake)
    assert err.value.type == "BudgetExceeded" and fake.calls == 0


def test_unimplemented_agent(provisioned_db, org):
    wf = seed_workflow(provisioned_db.owner_conninfo, org)["workflow"]
    with pytest.raises(ApplicationError) as err:
        run_step(provisioned_db, org, wf, "supervisor", {}, FakeModel())  # decided by the orchestrator
    assert err.value.type == "AgentNotAvailable"
    assert Decimal(sql(provisioned_db, "SELECT spent_usd FROM workflows WHERE id = %s", (wf,))[0][0]) == 0
