"""A crashed agent step resumes against PostgreSQL (migration 0006): same run, no repeated work."""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from moto import mock_aws

from aieos.agents.checkpoints import PostgresCheckpoints
from aieos.agents.runner import AgentRunner
from aieos.artifacts.store import ArtifactStore, build_s3_client
from aieos.cli import admin
from aieos.contracts.steps import AgentStepRequest
from aieos.core.settings import StorageSettings
from aieos.gateway.catalog import load_catalog
from aieos.gateway.handlers import artifact_handlers
from aieos.gateway.service import ToolGateway
from aieos.orchestration.activities import OrchestrationActivities
from aieos.persistence.db import Database
from aieos.providers.metering import PostgresUsageRecorder
from aieos.providers.router import ModelRouter
from tests.fakes import REQUEST, FakeModel, function_call, json_output, spec_payload
from tests.integration.conftest import ENGINEERING, seed_workflow
from tests.integration.test_agent_runner import DirectInvoker
from tests.unit.test_providers import routing

BUCKET = "resume-test"


# seed_workflow creates an unrelated run of its own; only the step's runs matter here.
STEP_RUNS = "SELECT {cols} FROM agent_runs WHERE workflow_id = %s AND step_key = 'requirements'"


class WorkerDied(RuntimeError):
    pass


@pytest.fixture(scope="module")
def org(provisioned_db, published_agents):
    return admin.create_org(
        provisioned_db.owner_conninfo, slug="resume", name="R", owner_sub="rs-owner", owner_email="o@rs.test"
    )


def attempt_step(provisioned_db, org_id, workflow_id, fake):
    with mock_aws():
        s3 = build_s3_client(StorageSettings(s3_artifact_bucket=BUCKET, _env_file=None))
        s3.create_bucket(Bucket=BUCKET)

        async def main():
            db = Database(provisioned_db.app_async_url, pool_size=3, max_overflow=0)
            try:
                catalog = load_catalog(ENGINEERING / "tool-catalog.yaml")
                gateway = ToolGateway(db, catalog, artifact_handlers(ArtifactStore(db, s3, bucket=BUCKET)))
                runner = AgentRunner(
                    db,
                    router=ModelRouter(routing(), model_factory=lambda *_: fake),
                    recorder=PostgresUsageRecorder(db),
                    invoker=DirectInvoker(gateway),
                    engineering_dir=ENGINEERING,
                    catalog=catalog,
                    checkpoints=PostgresCheckpoints(db),
                )
                return await OrchestrationActivities(db, runner).run_agent_step(
                    AgentStepRequest(
                        org_id=org_id,
                        workflow_id=workflow_id,
                        agent_key="requirements",
                        step_key="requirements",
                        attempt=1,
                        inputs={"request": REQUEST},
                    )
                )
            finally:
                await db.dispose()

        return asyncio.run(main())


def sql(provisioned_db, q, params=()):
    with psycopg.connect(provisioned_db.owner_conninfo) as c:
        return c.execute(q, params).fetchall()


def test_crashed_step_resumes_in_the_same_run(provisioned_db, org):
    wf = seed_workflow(provisioned_db.owner_conninfo, org)["workflow"]

    missing = "00000000-0000-7000-8000-000000000000"
    first = FakeModel(function_call("artifacts.read", {"artifact_id": missing}), WorkerDied("worker lost"))
    with pytest.raises(WorkerDied):
        attempt_step(provisioned_db, org, wf, first)
    runs = sql(provisioned_db, STEP_RUNS.replace("{cols}", "id, status"), (wf,))
    assert len(runs) == 1 and runs[0][1] == "failed"
    run_id = runs[0][0]

    second = FakeModel(json_output(spec_payload()))
    result = attempt_step(provisioned_db, org, wf, second)
    assert result.agent_run_id == run_id and result.status == "completed"  # the same run, resumed
    assert second.calls == 1  # only the answer; the earlier turn was not re-requested
    assert sql(provisioned_db, STEP_RUNS.replace("{cols}", "status"), (wf,)) == [("succeeded",)]
    executions = sql(
        provisioned_db,
        "SELECT idempotency_key FROM tool_executions WHERE agent_run_id = %s AND tool_name = 'artifacts.read'",
        (run_id,),
    )
    assert len(executions) == 1 and executions[0][0].endswith("a1-t1")  # the tool ran once

    # Re-delivered after completion (e.g. the worker died before reporting): the recorded result is returned.
    third = FakeModel()
    again = attempt_step(provisioned_db, org, wf, third)
    assert third.calls == 0 and again.output == result.output


def test_checkpoints_are_tenant_isolated(provisioned_db, org):
    other = admin.create_org(
        provisioned_db.owner_conninfo, slug="resume-2", name="R2", owner_sub="rs2", owner_email="o@rs2.test"
    )
    wf = seed_workflow(provisioned_db.owner_conninfo, org)["workflow"]
    attempt_step(provisioned_db, org, wf, FakeModel(json_output(spec_payload())))
    run_id = sql(provisioned_db, STEP_RUNS.replace("{cols}", "id"), (wf,))[0][0]

    async def peek():
        db = Database(provisioned_db.app_async_url, pool_size=1, max_overflow=0)
        try:
            return await PostgresCheckpoints(db).load(other, run_id), await PostgresCheckpoints(db).load(org, run_id)
        finally:
            await db.dispose()

    theirs, ours = asyncio.run(peek())
    assert theirs is None and ours is not None and "result" in ours
