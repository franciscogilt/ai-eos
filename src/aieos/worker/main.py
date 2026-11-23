"""`aieos-worker`: run the agent worker or the tool-gateway worker.

The two run as separate processes with separate IAM roles: only the gateway worker
is given GitHub and deployment credentials (B13), and only the agent worker model keys.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from temporalio.worker import Worker

from aieos.agents.checkpoints import PostgresCheckpoints
from aieos.agents.runner import AgentRunner
from aieos.artifacts.store import ArtifactStore
from aieos.core.logging import configure_logging, get_logger
from aieos.core.settings import (
    GitHubSettings,
    ModelRoutingSettings,
    SandboxSettings,
    Settings,
    StorageSettings,
    TemporalSettings,
)
from aieos.gateway.activities import GatewayActivities
from aieos.gateway.catalog import load_catalog
from aieos.gateway.github_handlers import GitHubTools
from aieos.gateway.handlers import artifact_handlers
from aieos.gateway.sandbox_handlers import sandbox_handlers
from aieos.gateway.service import ToolGateway
from aieos.integrations.github.app import GitHubApp
from aieos.observability.telemetry import setup_tracing
from aieos.orchestration.activities import OrchestrationActivities
from aieos.orchestration.client import connect
from aieos.orchestration.tool_bridge import TemporalToolInvoker, ToolCallWorkflow
from aieos.orchestration.workflows import AGENT_QUEUE, GATEWAY_QUEUE, FeatureDeliveryWorkflow
from aieos.persistence.db import Database
from aieos.providers.config import load_routing_config
from aieos.providers.metering import PostgresUsageRecorder
from aieos.providers.router import ModelRouter
from aieos.sandbox.docker import DockerCLI, DockerSandboxManager, SandboxLimits

log = get_logger("aieos.worker")


async def _run(kind: str, engineering: Path) -> None:
    # Workers reuse the API settings for the database and logging; OIDC values are not used here.
    settings = Settings()
    configure_logging(level=settings.log_level, json=settings.log_json)
    provider = setup_tracing(
        # OTEL_SERVICE_NAME is set per service in deployment; the default names the worker kind.
        service_name=settings.otel_service_name if "OTEL_SERVICE_NAME" in os.environ else f"aieos-{kind}",
        environment=settings.environment,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
    )
    db = Database(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        statement_timeout_ms=settings.db_statement_timeout_ms,
    )
    await db.assert_runtime_role_is_safe()
    client = await connect(TemporalSettings())
    if kind == "agents":
        router = ModelRouter(load_routing_config(ModelRoutingSettings().model_routing_path))
        runner = AgentRunner(
            db,
            router=router,
            recorder=PostgresUsageRecorder(db),
            invoker=TemporalToolInvoker(client),
            engineering_dir=engineering,
            checkpoints=PostgresCheckpoints(db),
        )
        acts = OrchestrationActivities(db, runner)
        worker = Worker(
            client, task_queue=AGENT_QUEUE, workflows=[FeatureDeliveryWorkflow, ToolCallWorkflow], activities=acts.all()
        )
    else:
        store = ArtifactStore.from_settings(db, StorageSettings())
        handlers = artifact_handlers(store)
        sandbox_settings = SandboxSettings()
        sandboxes = None
        if sandbox_settings.sandbox_backend == "docker":
            sandboxes = DockerSandboxManager(
                DockerCLI(),
                image=sandbox_settings.sandbox_image,
                runtime=sandbox_settings.sandbox_runtime,
                limits=SandboxLimits(
                    cpus=sandbox_settings.sandbox_default_cpus,
                    memory_mb=sandbox_settings.sandbox_default_memory_mb,
                    command_timeout_seconds=sandbox_settings.sandbox_default_timeout_seconds,
                ),
            )
            await sandboxes.reap(older_than_seconds=sandbox_settings.sandbox_max_age_seconds)
        github_settings = GitHubSettings()
        github: GitHubTools | None = None
        if github_settings.configured:
            app = GitHubApp(
                str(github_settings.github_app_id),
                github_settings.private_key_pem(),
                api_url=github_settings.github_api_url,
            )
            github = GitHubTools(db, app, store, sandboxes)
            handlers |= github.handlers()
        else:
            log.warning("github.not_configured", detail="repo.* and github.* tools will fail with 'no handler'")
        if sandboxes is not None:
            handlers |= sandbox_handlers(sandboxes, store, snapshot=github.snapshot_for if github else None)
        gateway = ToolGateway(db, load_catalog(engineering / "tool-catalog.yaml"), handlers)
        worker = Worker(client, task_queue=GATEWAY_QUEUE, activities=GatewayActivities(gateway, sandboxes).all())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("worker.started", kind=kind)
    async with worker:
        await stop.wait()  # graceful: in-flight activities finish or are retried elsewhere
    await db.dispose()
    if provider is not None:
        provider.shutdown()  # flush spans still in the batch queue
    log.info("worker.stopped", kind=kind)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aieos-worker")
    parser.add_argument("kind", choices=["agents", "gateway"])
    parser.add_argument("--engineering", type=Path, default=Path("engineering"))
    args = parser.parse_args(argv)
    asyncio.run(_run(args.kind, args.engineering))
    return 0


if __name__ == "__main__":
    sys.exit(main())
