"""AgentRunner: executes one agent step for the orchestrator (`run_agent_step` activity).

For the run it is given:
  1. Load the run's published agent version and verify that the deployed contract and
     prompt are byte-identical to it (by SHA-256). A drifted deployment is refused.
  2. Build the run's pinned, metered model (workflow and per-run budgets, provider fallback).
  3. Build the agent with gateway-backed tools, run it with grounding retries.
  4. Store the output as an artifact through the gateway; return a compact step result.

Implemented agents: requirements, architecture (here), developer and qa_security (`delivery`).
The supervisor's decisions are made by the deterministic orchestrator, so it has no step here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text

from aieos.agents.checkpoints import Checkpoints
from aieos.agents.delivery import prepare_developer, prepare_qa_security
from aieos.agents.errors import AgentNotAvailableError, ContractViolationError, ModelRequestRejectedError
from aieos.agents.factory import RunScope, ToolInvoker, build_agent, run_with_retries
from aieos.agents.grounding import architecture_violations, requirement_grounding_violations
from aieos.agents.loop import AgentDefinition
from aieos.agents.registry import LoadedContract, load_all
from aieos.agents.steps import AsyncPreparer, Prepared, StepTools, resolve
from aieos.agents.untrusted import wrap_untrusted
from aieos.contracts.io import ArchitectureDesign, RequirementsInput, RequirementsSpec
from aieos.contracts.steps import AgentStepRequest, AgentStepResult
from aieos.contracts.tools import ToolRequest
from aieos.core.logging import get_logger
from aieos.gateway.catalog import ToolCatalog, load_catalog
from aieos.persistence.db import Database
from aieos.providers.llm import ProviderRequestError
from aieos.providers.metering import MeteringContext, UsageRecorder
from aieos.providers.router import ModelNotConfiguredError, ModelRouter

log = get_logger(__name__)


def prepare_requirements(inputs: dict[str, Any], scope: RunScope) -> Prepared:
    data = RequirementsInput.model_validate(
        {"request": inputs.get("request"), "clarifications": inputs.get("clarifications") or {}}
    )
    message = "Analyse this software request and produce the RequirementsSpec.\n\n" + wrap_untrusted(
        "user_request", data.request
    )
    if data.clarifications:
        message += (
            "\n\nAnswers to your earlier questions (cite them as clarification sources by id):\n"
            + wrap_untrusted("clarifications", json.dumps(data.clarifications, ensure_ascii=False, indent=2))
        )

    def validate(spec: RequirementsSpec) -> list[str]:
        return requirement_grounding_violations(
            spec,
            request=data.request,
            clarification_ids=set(data.clarifications),
            repository_paths=scope.repository_paths_read,
        )

    def summary(spec: RequirementsSpec) -> str:
        blocking = sum(q.blocking for q in spec.open_questions)
        return (
            f"{len(spec.requirements)} requirements, {len(spec.acceptance_criteria)} criteria, "
            f"{blocking} blocking questions"
        )

    return Prepared(
        message, RequirementsSpec, validate, lambda s: s.model_dump(mode="json"), summary, "requirements_spec"
    )


def prepare_architecture(inputs: dict[str, Any], scope: RunScope) -> Prepared:
    try:
        spec = RequirementsSpec.model_validate(inputs.get("requirements"))
    except ValidationError as exc:
        raise ContractViolationError(f"architecture step needs a valid RequirementsSpec: {exc}") from None
    if spec.has_blocking_questions:
        raise ContractViolationError("architecture cannot start while requirements have blocking questions")
    message = "Design the change for this approved specification and propose tasks.\n\n" + wrap_untrusted(
        "requirements_spec", spec.model_dump_json(indent=2)
    )

    def to_output(design: ArchitectureDesign) -> dict[str, Any]:
        out = design.model_dump(mode="json")
        out["tasks"] = [
            {"key": t.key, "title": t.title, "description": t.description, "depends_on": t.depends_on}
            for t in design.proposed_tasks
        ]
        return out

    return Prepared(
        message,
        ArchitectureDesign,
        lambda d: architecture_violations(d, spec),
        to_output,
        lambda d: f"{len(d.components)} components, {len(d.proposed_tasks)} tasks, {len(d.decisions)} decisions",
        "architecture_design",
    )


# Steps that need no tools to prepare (also used by the eval harness).
PREPARERS: dict[str, Callable[[dict[str, Any], RunScope], Prepared]] = {
    "requirements": prepare_requirements,
    "architecture": prepare_architecture,
}
# Steps whose preparation and checks use the sandbox through the gateway.
DELIVERY_PREPARERS: dict[str, AsyncPreparer] = {
    "developer": prepare_developer,
    "qa_security": prepare_qa_security,
}


class AgentRunner:
    def __init__(
        self,
        db: Database,
        *,
        router: ModelRouter,
        recorder: UsageRecorder,
        invoker: ToolInvoker,
        engineering_dir: Path,
        catalog: ToolCatalog | None = None,
        checkpoints: Checkpoints | None = None,
    ) -> None:
        self._db = db
        self._checkpoints = checkpoints
        self._router = router
        self._recorder = recorder
        self._invoker = invoker
        self._catalog = catalog or load_catalog(engineering_dir / "tool-catalog.yaml")
        self._contracts = load_all(
            engineering_dir / "agent-definitions", prompts_dir=engineering_dir / "prompts", catalog=self._catalog
        )

    async def _published(self, org_id: UUID, agent_run_id: UUID) -> Any:
        async with self._db.tenant(org_id) as conn:
            row = (
                await conn.execute(
                    text(
                        """SELECT a.key, v.version, v.contract_sha256, v.prompt_sha256
                   FROM agent_runs r JOIN agent_versions v ON v.id = r.agent_version_id
                   JOIN agents a ON a.id = v.agent_id WHERE r.id = :run"""
                    ),
                    {"run": agent_run_id},
                )
            ).first()
        if row is None:
            raise ContractViolationError(f"agent run {agent_run_id} not found")
        return row

    def _verified(self, row: Any) -> LoadedContract:
        loaded = self._contracts.get(row.key)
        if loaded is None or loaded.contract.version != row.version:
            raise ContractViolationError(f"{row.key}@{row.version} is not the version deployed on this worker")
        if (loaded.contract_sha256, loaded.prompt_sha256) != (row.contract_sha256, row.prompt_sha256):
            raise ContractViolationError(
                f"{row.key}@{row.version}: deployed contract or prompt differs from the published version"
            )
        return loaded

    async def run(self, request: AgentStepRequest, *, agent_run_id: UUID) -> AgentStepResult:
        if request.agent_key not in PREPARERS and request.agent_key not in DELIVERY_PREPARERS:
            raise AgentNotAvailableError(f"agent {request.agent_key!r} has no step runner")
        loaded = self._verified(await self._published(request.org_id, agent_run_id))
        contract = loaded.contract
        state = await self._checkpoints.load(request.org_id, agent_run_id) if self._checkpoints else None
        if state and state.get("result"):
            # The step finished before the worker died; return the recorded result.
            log.info("agent.step_already_completed", agent=contract.key, agent_run_id=str(agent_run_id))
            return AgentStepResult.model_validate(state["result"])
        ctx = MeteringContext(
            org_id=request.org_id,
            workflow_id=request.workflow_id,
            agent_run_id=agent_run_id,
            agent_key=contract.key,
            run_max_cost_usd=Decimal(str(contract.limits.max_cost_usd)),
            run_max_tokens=contract.limits.max_total_tokens,
        )
        try:
            model = self._router.model_for_run(contract, ctx, self._recorder)
        except ModelNotConfiguredError as exc:
            raise AgentNotAvailableError(f"{contract.key}: {exc}") from None

        scope = RunScope(
            org_id=request.org_id,
            workflow_id=request.workflow_id,
            agent_run_id=agent_run_id,
            max_tool_calls=contract.limits.max_tool_calls,
        )
        if request.agent_key in PREPARERS:
            prepared = PREPARERS[request.agent_key](request.inputs, scope)
        else:
            prepared = await DELIVERY_PREPARERS[request.agent_key](
                request.inputs, scope, StepTools(self._invoker, scope)
            )

        def make_agent(attempt: int) -> AgentDefinition:
            scope.attempt = attempt
            scope.repository_paths_read.clear()  # grounding counts only files read in the accepted attempt
            return build_agent(
                loaded, catalog=self._catalog, invoker=self._invoker, scope=scope, output_type=prepared.output_type
            )

        retries = (
            contract.failure_handling.max_retries if "schema_violation" in contract.failure_handling.retry_on else 0
        )
        try:
            output, attempts = await run_with_retries(
                make_agent,
                prepared.message,
                model=model,
                output_type=prepared.output_type,
                validate=prepared.validate,
                max_turns=contract.limits.max_turns,
                timeout_seconds=contract.limits.timeout_seconds,
                max_retries=retries,
                scope=scope,
                resume=state,
                save=self._saver(request.org_id, agent_run_id),
            )
        except ProviderRequestError as exc:
            raise ModelRequestRejectedError(str(exc)) from None

        payload = await resolve(prepared.to_output(output))
        artifact_ids: list[UUID] = []
        stored = await self._invoker.invoke(
            ToolRequest(
                org_id=request.org_id,
                workflow_id=request.workflow_id,
                agent_run_id=agent_run_id,
                tool_name="artifacts.write",
                call_id="output",
                arguments={
                    "kind": prepared.artifact_kind,
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                    "content_type": "application/json",
                },
            )
        )
        if stored.status == "succeeded" and stored.result:
            artifact_ids.append(UUID(stored.result["artifact_id"]))
        else:
            log.warning("agent.output_not_stored", agent=contract.key, status=stored.status, reason=stored.reason)
        log.info("agent.step_completed", agent=contract.key, attempts=attempts, tool_calls=scope.tool_calls)
        result = AgentStepResult(
            status="blocked" if prepared.status(output) == "blocked" else "completed",
            summary=prepared.summary(output),
            output=payload,
            artifact_ids=artifact_ids,
            agent_run_id=agent_run_id,
        )
        if self._checkpoints is not None:
            await self._checkpoints.save(request.org_id, agent_run_id, {"result": result.model_dump(mode="json")})
        return result

    def _saver(self, org_id: UUID, agent_run_id: UUID) -> Any:
        if self._checkpoints is None:
            return None
        checkpoints = self._checkpoints

        async def save(state: dict[str, Any]) -> None:
            await checkpoints.save(org_id, agent_run_id, state)

        return save
