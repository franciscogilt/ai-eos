"""Developer and QA/Security steps (B15).

Neither agent's word is taken for the facts that gate delivery:
* Developer: after the model submits, the platform runs the test suite in the developer's sandbox
  and exports the patch itself. Failing tests, an empty patch, a patch that breaks push policy, or
  no test file changed are returned to the model as reasons and the attempt is retried. The step's
  `DeveloperResult` (patch and test report) is built from those platform results.
* QA/Security: the platform prepares a fresh sandbox (approved patches committed, the patch under
  review applied), runs the tests and the security scan *before* the model starts, and gives the
  results to the model as evidence. An approval is rejected if the platform's tests failed, if the
  scan found secrets, or if the report does not cover exactly the task's acceptance criteria.

Sandboxes are stacked: a task's sandbox starts from the base commit plus every patch already
approved in this workflow, so tasks build on each other and the final push applies them in order.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from aieos.agents.errors import AgentStepError, ContractViolationError
from aieos.agents.factory import RunScope
from aieos.agents.steps import Prepared, StepTools
from aieos.agents.untrusted import wrap_untrusted
from aieos.contracts.io import (
    ArchitectureDesign,
    ArtifactRef,
    DeveloperResult,
    DeveloperSubmission,
    PlannedTask,
    RequirementsSpec,
    ReviewReport,
)
from aieos.contracts.tools import ToolResult

MAX_PATCH_IN_PROMPT = 120_000
_TEST_HINTS = ("test", "spec")


class StepSetupError(AgentStepError):
    """The platform could not prepare the step (sandbox, patches). Not a model failure."""

    error_type = "StepSetupFailed"


def _parse_context(inputs: dict[str, Any]) -> tuple[PlannedTask, RequirementsSpec, ArchitectureDesign]:
    try:
        spec = RequirementsSpec.model_validate(inputs.get("requirements"))
        design = ArchitectureDesign.model_validate(
            {k: v for k, v in (inputs.get("design") or {}).items() if k != "tasks"}
        )
    except ValidationError as exc:
        raise ContractViolationError(f"step needs the approved requirements and design: {exc}") from None
    key = (inputs.get("task") or {}).get("key")
    task = next((t for t in design.proposed_tasks if t.key == key), None)
    if task is None:
        raise ContractViolationError(f"task {key!r} is not in the design")
    return task, spec, design


def _ids(inputs: dict[str, Any], name: str) -> list[str]:
    value = inputs.get(name) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ContractViolationError(f"{name} must be a list of artifact ids")
    return value


def _task_brief(task: PlannedTask, spec: RequirementsSpec, design: ArchitectureDesign) -> str:
    criteria = [c for c in spec.acceptance_criteria if c.id in task.acceptance_criteria_ids]
    brief = {
        "task": task.model_dump(mode="json"),
        "acceptance_criteria": [c.model_dump(mode="json") for c in criteria],
        "design_overview": design.overview,
        "components": [c.model_dump(mode="json") for c in design.components],
        "security_considerations": design.security_considerations,
    }
    return json.dumps(brief, ensure_ascii=False, indent=2)


async def _required(tools: StepTools, tool: str, args: dict[str, Any], call_id: str) -> ToolResult:
    result = await tools.call(tool, args, call_id)
    if result.status != "succeeded":
        raise StepSetupError(f"{tool} {result.status}: {result.reason}")
    return result


def _tail(text: Any, limit: int = 3000) -> str:
    s = str(text or "")
    return s[-limit:]


def _ref(result: dict[str, Any], kind: str) -> ArtifactRef:
    return ArtifactRef(artifact_id=str(result["artifact_id"]), kind=kind, sha256=str(result["sha256"]))


async def _store_json(tools: StepTools, kind: str, payload: Any, call_id: str) -> ArtifactRef:
    stored = await _required(
        tools,
        "artifacts.write",
        {"kind": kind, "content": json.dumps(payload, indent=2, default=str), "content_type": "application/json"},
        call_id,
    )
    assert stored.result is not None
    return _ref(stored.result, kind)


# ------------------------------------------------------------------ developer
async def prepare_developer(inputs: dict[str, Any], scope: RunScope, tools: StepTools) -> Prepared:
    task, spec, design = _parse_context(inputs)
    base_patches = _ids(inputs, "base_patches")
    previous = inputs.get("previous_patch")
    await _required(
        tools,
        "sandbox.prepare",
        {"base_patches": base_patches, "apply_patches": [previous] if previous else []},
        "prepare",
    )
    message = "Implement this task in your sandbox.\n\n" + wrap_untrusted("task", _task_brief(task, spec, design))
    if previous:
        message += (
            "\n\nYour previous attempt is already applied in the workspace (uncommitted). "
            "Change it to address the review findings below; `git diff` shows it."
        )
    feedback = inputs.get("feedback") or []
    if feedback:
        message += "\n\nIndependent review findings to fix:\n" + wrap_untrusted(
            "review_findings", json.dumps(feedback, ensure_ascii=False, indent=2)[:20_000]
        )
    message += (
        f"\n\nWhen done, call submit_output. The platform then runs the full test suite and exports the patch "
        f"itself; your answer is rejected if tests fail or no test file changed. Acceptance criteria: "
        f"{', '.join(task.acceptance_criteria_ids)}."
    )

    evidence: dict[int, dict[str, Any]] = {}

    async def validate(sub: DeveloperSubmission) -> list[str]:
        if sub.status == "blocked":
            return []
        attempt = scope.attempt
        tests = await _required(tools, "sandbox.run_tests", {"selector": None}, f"verify-tests-a{attempt}")
        exported = await _required(tools, "sandbox.export_patch", {"summary": sub.summary}, f"export-a{attempt}")
        t, e = tests.result or {}, exported.result or {}
        evidence[attempt] = {"tests": t, "export": e}
        problems: list[str] = []
        if not t.get("passed"):
            problems.append(
                f"the platform's test run failed (exit code {t.get('exit_code')}, timed out: {t.get('timed_out')}). "
                f"Output tail:\n{_tail(t.get('stdout'))}\n{_tail(t.get('stderr'), 1000)}"
            )
        if e.get("empty"):
            problems.append("the workspace has no changes; implement the task before submitting")
        problems += [f"patch policy: {v}" for v in e.get("policy_violations", [])]
        files = [str(f) for f in e.get("files_changed", [])]
        if files and not any(h in f.lower() for f in files for h in _TEST_HINTS):
            problems.append("no test file was added or changed; add tests that prove the acceptance criteria")
        return problems

    async def to_output(sub: DeveloperSubmission) -> dict[str, Any]:
        if sub.status == "blocked":
            result = DeveloperResult(status="blocked", summary=sub.summary, blocked_reason=sub.blocked_reason)
            return result.model_dump(mode="json")
        ev = evidence[scope.attempt]
        report = await _store_json(tools, "test_report", ev["tests"], f"test-report-a{scope.attempt}")
        result = DeveloperResult(
            status="implemented",
            summary=sub.summary,
            patch=_ref(ev["export"], "patch"),
            tests_added=sub.tests_added,
            test_report=report,
            files_changed=[str(f) for f in ev["export"].get("files_changed", [])],
        )
        return result.model_dump(mode="json")

    return Prepared(
        message=message,
        output_type=DeveloperSubmission,
        validate=validate,
        to_output=to_output,
        summary=lambda s: s.summary[:300] if s.status == "implemented" else f"blocked: {s.blocked_reason}"[:300],
        artifact_kind="agent_output",
        status=lambda s: "blocked" if s.status == "blocked" else "completed",
    )


# ------------------------------------------------------------------ QA / security
async def prepare_qa_security(inputs: dict[str, Any], scope: RunScope, tools: StepTools) -> Prepared:
    task, spec, design = _parse_context(inputs)
    base_patches = _ids(inputs, "base_patches")
    patch_id = inputs.get("patch_artifact_id")
    if not isinstance(patch_id, str):
        raise ContractViolationError("review needs patch_artifact_id")
    await _required(tools, "sandbox.prepare", {"base_patches": base_patches, "apply_patches": [patch_id]}, "prepare")
    patch = await _required(tools, "artifacts.read", {"artifact_id": patch_id}, "read-patch")
    tests = await _required(tools, "sandbox.run_tests", {"selector": None}, "platform-tests")
    scan = await _required(tools, "sandbox.run_security_scan", {"scanners": None}, "platform-scan")
    t, sc = tests.result or {}, scan.result or {}
    secrets = [f for f in sc.get("findings", []) if f.get("tool") == "secrets"]
    criteria = list(task.acceptance_criteria_ids)

    diff_text = str((patch.result or {}).get("text") or "")
    if len(diff_text) > MAX_PATCH_IN_PROMPT or (patch.result or {}).get("truncated"):
        diff_note = "The patch is too large to include; inspect it with `git diff` in your sandbox."
        diff_text = diff_text[:MAX_PATCH_IN_PROMPT]
    else:
        diff_note = "The full patch follows (also applied, uncommitted, in your sandbox)."
    platform = {
        "tests": {k: t.get(k) for k in ("passed", "exit_code", "timed_out", "duration_ms")},
        "tests_output_tail": _tail(t.get("stdout")),
        "security_scan": {k: sc.get(k) for k in ("scanners", "findings", "truncated", "not_run")},
    }
    message = (
        "Review this patch independently. You did not write it.\n\n"
        + wrap_untrusted("task", _task_brief(task, spec, design))
        + "\n\nDeveloper's summary (a claim, not evidence):\n"
        + wrap_untrusted("developer_summary", str(inputs.get("developer_summary") or "")[:4000])
        + "\n\nPlatform results, run before you started (trustworthy):\n"
        + json.dumps(platform, ensure_ascii=False, indent=2)
        + f"\n\n{diff_note}\n"
        + wrap_untrusted("patch", diff_text)
        + f"\n\nReport every acceptance criterion exactly once: {', '.join(criteria)}."
    )

    def validate(report: ReviewReport) -> list[str]:
        problems: list[str] = []
        got = set(report.acceptance_criteria_results)
        if got != set(criteria):
            problems.append(
                f"acceptance_criteria_results must have exactly these keys: {', '.join(criteria)} "
                f"(missing: {sorted(set(criteria) - got)}, unknown: {sorted(got - set(criteria))})"
            )
        if report.verdict == "approve" and not t.get("passed"):
            problems.append("cannot approve: the platform's test run of this patch failed")
        if report.verdict == "approve" and secrets:
            problems.append(f"cannot approve: the secret scan found {len(secrets)} likely secret(s) in added lines")
        return problems

    async def to_output(report: ReviewReport) -> dict[str, Any]:
        test_ref = await _store_json(tools, "test_report", t, "review-test-report")
        security_ref = None
        if sc.get("report_artifact_id") and sc.get("report_sha256"):
            security_ref = ArtifactRef(
                artifact_id=str(sc["report_artifact_id"]), kind="security_report", sha256=str(sc["report_sha256"])
            )
        final = report.model_copy(update={"test_report": test_ref, "security_report": security_ref})
        out = final.model_dump(mode="json")
        out["platform_tests_passed"] = bool(t.get("passed"))
        out["reviewed_patch_artifact_id"] = patch_id
        return out

    def summary(report: ReviewReport) -> str:
        met = sum(v == "met" for v in report.acceptance_criteria_results.values())
        return f"{report.verdict}: {met}/{len(criteria)} criteria met, {len(report.findings)} findings"

    return Prepared(message, ReviewReport, validate, to_output, summary, "review_report")
