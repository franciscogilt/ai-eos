"""`aieos-eval`: golden-set evaluation of agents against real models.

Uses the production path (contract, prompt, factory, grounding retries, metered model) with
tools disabled and a hard USD cap for the whole suite. No database, sandbox or GitHub is needed.

    aieos-eval --agent requirements --cases engineering/evals/requirements --max-usd 2 --report eval.json
    aieos-eval --agent qa_security --cases engineering/evals/qa_security

QA/Security cases describe the patch under review and the platform's evidence (test run, security
scan) in `context`; the platform steps are scripted from it (`ScriptedPlatform`), so the model is
judged on its review alone. A `_shared.yaml` in a cases directory provides default `context`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from aieos.agents.delivery import prepare_qa_security
from aieos.agents.errors import AgentStepError
from aieos.agents.factory import RunScope, build_agent, run_with_retries
from aieos.agents.registry import LoadedContract, load_all
from aieos.agents.runner import PREPARERS
from aieos.agents.steps import StepTools, resolve
from aieos.contracts.io import RequirementsSpec, ReviewReport
from aieos.contracts.tools import ToolRequest, ToolResult
from aieos.gateway.catalog import ToolCatalog, load_catalog
from aieos.providers.config import load_routing_config
from aieos.providers.llm import ProviderError
from aieos.providers.metering import BudgetExceededError, BudgetStatus, MeteringContext
from aieos.providers.models import ModelUnavailableError
from aieos.providers.pricing import TokenUsage
from aieos.providers.router import ModelRouter


@dataclass
class EvalCase:
    id: str
    agent: str
    request: str = ""
    clarifications: dict[str, str] = field(default_factory=dict)
    expect: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    checks: list[Check]
    attempts: int = 0
    error: str | None = None


class SuiteBudget:
    """UsageRecorder with one USD cap for the whole suite."""

    def __init__(self, max_usd: Decimal) -> None:
        self.max_usd = max_usd
        self.spent = Decimal(0)

    async def check_budget(self, ctx: MeteringContext) -> BudgetStatus:
        if self.spent >= self.max_usd:
            raise BudgetExceededError(f"eval budget of ${self.max_usd} exhausted")
        return BudgetStatus(self.spent, self.max_usd, 0, 10**12, self.spent, 0)

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
        self.spent += cost_usd


class NoTools:
    async def invoke(self, request: ToolRequest) -> ToolResult:
        return ToolResult(status="failed", reason="tools are disabled in evals; answer from the input only")


class ScriptedPlatform:
    """The platform side of a QA step, from a case's `context`. Model tool calls are refused."""

    def __init__(self, context: dict[str, Any]) -> None:
        self._context = context

    async def invoke(self, request: ToolRequest) -> ToolResult:
        c = self._context
        if request.call_id == "prepare":
            return ToolResult(status="succeeded", reason="ok", result={})
        if request.call_id == "read-patch":
            return ToolResult(status="succeeded", reason="ok", result={"text": c.get("patch", "")})
        if request.call_id == "platform-tests":
            tests = {"passed": True, "exit_code": 0, "timed_out": False, "stdout": "", **c.get("tests", {})}
            return ToolResult(status="succeeded", reason="ok", result=tests)
        if request.call_id == "platform-scan":
            scan = {"scanners": ["secrets", "bandit"], "findings": c.get("scan_findings", []), "not_run": []}
            return ToolResult(status="succeeded", reason="ok", result=scan)
        if request.tool_name == "artifacts.write":
            return ToolResult(
                status="succeeded", reason="ok", result={"artifact_id": str(uuid.uuid4()), "sha256": "0" * 64}
            )
        return ToolResult(status="failed", reason="tools are disabled in evals; review from the evidence given")


def check_review(report: ReviewReport, expect: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    if "verdict" in expect:
        checks.append(
            Check("verdict", report.verdict == expect["verdict"], f"expected {expect['verdict']}, got {report.verdict}")
        )
    for criterion, want in (expect.get("criteria") or {}).items():
        got = report.acceptance_criteria_results.get(criterion)
        checks.append(Check(f"criterion:{criterion}", got == want, f"expected {want}, got {got}"))
    categories = expect.get("finding_category_any")
    if categories:
        order = ["info", "low", "medium", "high", "critical"]
        floor = order.index(expect.get("min_severity", "medium"))
        hits = [f for f in report.findings if f.category in categories and order.index(f.severity) >= floor]
        checks.append(
            Check("finding", bool(hits), f"needs a {'/'.join(categories)} finding of severity >= {order[floor]}")
        )
    return checks


def load_cases(directory: Path) -> list[EvalCase]:
    shared_path = directory / "_shared.yaml"
    shared = yaml.safe_load(shared_path.read_text(encoding="utf-8")) if shared_path.exists() else {}
    cases = []
    for p in sorted(directory.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        raw["context"] = {**(shared.get("context") or {}), **(raw.get("context") or {})}
        cases.append(EvalCase(**raw))
    if len({c.id for c in cases}) != len(cases):
        raise ValueError("duplicate eval case ids")
    return cases


def check_requirements(spec: RequirementsSpec, expect: dict[str, Any]) -> list[Check]:
    statements = [r.statement.lower() for r in spec.requirements]
    criteria = [f"{a.given} {a.when} {a.then}".lower() for a in spec.acceptance_criteria]
    text = " ".join(statements + criteria)
    checks = [Check("grounded", True, "every source verified against the input")]
    if "blocking_questions" in expect:
        want = bool(expect["blocking_questions"])
        checks.append(
            Check(
                "blocking_questions",
                spec.has_blocking_questions == want,
                f"expected {want}, got {spec.has_blocking_questions}",
            )
        )
    if "min_requirements" in expect:
        n = int(expect["min_requirements"])
        checks.append(Check("min_requirements", len(spec.requirements) >= n, f"{len(spec.requirements)} >= {n}"))
    for term in expect.get("forbidden_terms", []):
        checks.append(Check(f"forbidden:{term}", term.lower() not in text, "must not appear in requirements"))
    for term in expect.get("must_mention", []):
        checks.append(Check(f"mentions:{term}", term.lower() in text, "must appear in requirements or criteria"))
    return checks


async def run_case(
    case: EvalCase, *, loaded: LoadedContract, catalog: ToolCatalog, router: ModelRouter, budget: SuiteBudget
) -> CaseResult:
    contract = loaded.contract
    ids = {k: uuid.uuid4() for k in ("org", "wf", "run")}
    ctx = MeteringContext(
        org_id=ids["org"],
        workflow_id=ids["wf"],
        agent_run_id=ids["run"],
        agent_key=contract.key,
        run_max_cost_usd=Decimal(str(contract.limits.max_cost_usd)),
        run_max_tokens=contract.limits.max_total_tokens,
    )
    scope = RunScope(
        org_id=ids["org"], workflow_id=ids["wf"], agent_run_id=ids["run"], max_tool_calls=contract.limits.max_tool_calls
    )
    invoker: Any = NoTools()
    if case.agent == "qa_security":
        invoker = ScriptedPlatform(case.context)
        inputs = {
            **case.context,
            "base_patches": [],
            "patch_artifact_id": str(uuid.uuid4()),
            "author_agent_run_id": str(uuid.uuid4()),
        }
        prepared = await prepare_qa_security(inputs, scope, StepTools(invoker, scope))
    else:
        prepared = PREPARERS[case.agent]({"request": case.request, "clarifications": case.clarifications}, scope)
    try:
        output, attempts = await run_with_retries(
            lambda attempt: build_agent(
                loaded, catalog=catalog, invoker=invoker, scope=scope, output_type=prepared.output_type
            ),
            prepared.message,
            model=router.model_for_run(contract, ctx, budget),
            output_type=prepared.output_type,
            validate=prepared.validate,
            max_turns=contract.limits.max_turns,
            timeout_seconds=contract.limits.timeout_seconds,
            max_retries=contract.failure_handling.max_retries,
        )
    except (AgentStepError, BudgetExceededError, ProviderError, ModelUnavailableError) as exc:
        return CaseResult(case.id, False, [Check("completed", False, str(exc))], error=type(exc).__name__)
    if case.agent == "requirements":
        checks = check_requirements(output, case.expect)
    elif case.agent == "qa_security":
        await resolve(prepared.to_output(output))  # same post-processing as production
        checks = check_review(output, case.expect)
    else:
        checks = []
    if "max_attempts" in case.expect:
        checks.append(Check("max_attempts", attempts <= int(case.expect["max_attempts"]), f"took {attempts}"))
    return CaseResult(case.id, all(c.passed for c in checks), checks, attempts)


async def run_suite(
    cases: list[EvalCase], *, engineering: Path, router: ModelRouter, budget: SuiteBudget
) -> list[CaseResult]:
    catalog = load_catalog(engineering / "tool-catalog.yaml")
    contracts = load_all(engineering / "agent-definitions", prompts_dir=engineering / "prompts", catalog=catalog)
    return [await run_case(c, loaded=contracts[c.agent], catalog=catalog, router=router, budget=budget) for c in cases]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aieos-eval")
    parser.add_argument("--agent", required=True, choices=sorted({*PREPARERS, "qa_security"}))
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--engineering", type=Path, default=Path("engineering"))
    parser.add_argument(
        "--routing",
        type=Path,
        # Same setting the agent worker uses, so evals run on the models the platform will use.
        default=Path(os.environ.get("MODEL_ROUTING_PATH") or "engineering/model-routing.yaml"),
    )
    parser.add_argument("--max-usd", type=Decimal, default=Decimal("2"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    cases = [c for c in load_cases(args.cases) if c.agent == args.agent]
    budget = SuiteBudget(args.max_usd)
    results = asyncio.run(
        run_suite(
            cases, engineering=args.engineering, router=ModelRouter(load_routing_config(args.routing)), budget=budget
        )
    )
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.case_id}  attempts={r.attempts}")
        for c in r.checks:
            if not c.passed:
                print(f"      - {c.name}: {c.detail}")
    passed = sum(r.passed for r in results)
    print(f"\n{passed}/{len(results)} passed, spent ${budget.spent}")
    if args.report:
        args.report.write_text(
            json.dumps(
                {"agent": args.agent, "spent_usd": str(budget.spent), "results": [asdict(r) for r in results]}, indent=2
            )
        )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
