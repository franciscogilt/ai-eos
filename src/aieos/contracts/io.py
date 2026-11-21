"""Input/output schemas for the five MVP agents.

Agent outputs are parsed into these models (OpenAI Agents SDK `output_type`).
Validators encode rules that must not depend on the model obeying its prompt,
e.g. "the Requirements Agent must never invent requirements": every requirement
must cite where it came from.

Large content (diffs, logs, documents) travels as artifact references, never inline.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_ID = r"^[A-Z]{1,4}-\d{1,4}$"  # e.g. REQ-1, AC-3, T-12


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactRef(_Schema):
    artifact_id: str
    kind: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------- supervisor
class SupervisorInput(_Schema):
    workflow_id: str
    request: str = Field(min_length=1, max_length=20_000)
    repository_full_name: str
    completed_steps: list[str] = []
    latest_artifacts: list[ArtifactRef] = []


class SupervisorDecision(_Schema):
    next_action: Literal[
        "run_requirements",
        "ask_clarification",
        "run_architecture",
        "plan_tasks",
        "run_task",
        "run_review",
        "open_pull_request",
        "request_approval",
        "finish",
        "fail",
    ]
    reason: str = Field(min_length=1)
    task_key: str | None = None
    questions_for_user: list[str] = []

    @model_validator(mode="after")
    def _consistent(self) -> SupervisorDecision:
        if self.next_action == "ask_clarification" and not self.questions_for_user:
            raise ValueError("ask_clarification requires at least one question")
        if self.next_action == "run_task" and not self.task_key:
            raise ValueError("run_task requires task_key")
        return self


# --------------------------------------------------------------------------- requirements
class RequirementsInput(_Schema):
    request: str = Field(min_length=1, max_length=20_000)
    clarifications: dict[str, str] = Field(default_factory=dict, description="question id -> user answer")
    repository_summary: ArtifactRef | None = None


class SourceRef(_Schema):
    kind: Literal["request", "clarification", "repository"]
    ref: str = Field(min_length=1, description="quote, clarification id, or file path")


class Requirement(_Schema):
    id: str = Field(pattern=_ID)
    statement: str = Field(min_length=1)
    priority: Literal["must", "should", "could"]
    sources: list[SourceRef] = Field(min_length=1)  # no source -> invented -> rejected


class AcceptanceCriterion(_Schema):
    id: str = Field(pattern=_ID)
    requirement_ids: list[str] = Field(min_length=1)
    given: str
    when: str
    then: str


class OpenQuestion(_Schema):
    id: str = Field(pattern=_ID)
    question: str
    blocking: bool
    why_it_matters: str


class RequirementsSpec(_Schema):
    summary: str
    facts: list[str]
    assumptions: list[str]
    requirements: list[Requirement]
    acceptance_criteria: list[AcceptanceCriterion]
    open_questions: list[OpenQuestion]
    out_of_scope: list[str] = []

    @model_validator(mode="after")
    def _references_resolve(self) -> RequirementsSpec:
        req_ids = {r.id for r in self.requirements}
        if len(req_ids) != len(self.requirements):
            raise ValueError("duplicate requirement ids")
        for ac in self.acceptance_criteria:
            missing = set(ac.requirement_ids) - req_ids
            if missing:
                raise ValueError(f"{ac.id} references unknown requirements {sorted(missing)}")
        uncovered = {r.id for r in self.requirements if r.priority == "must"} - {
            rid for ac in self.acceptance_criteria for rid in ac.requirement_ids
        }
        if uncovered:
            raise ValueError(f"'must' requirements without acceptance criteria: {sorted(uncovered)}")
        return self

    @property
    def has_blocking_questions(self) -> bool:
        return any(q.blocking for q in self.open_questions)


# --------------------------------------------------------------------------- architecture
class ArchitectureInput(_Schema):
    requirements: ArtifactRef
    repository_summary: ArtifactRef | None = None
    standards: list[str] = []


class ComponentChange(_Schema):
    name: str
    responsibility: str
    change_type: Literal["new", "modified", "unchanged_dependency"]
    files_expected: list[str] = []


class ArchitectureDecision(_Schema):
    id: str = Field(pattern=_ID)
    title: str
    context: str
    decision: str
    alternatives_considered: list[str]
    consequences: str


class ArchitectureDesign(_Schema):
    overview: str
    components: list[ComponentChange] = Field(min_length=1)
    interfaces: list[str]
    data_model_changes: list[str]
    decisions: list[ArchitectureDecision]
    security_considerations: list[str] = Field(min_length=1)
    risks: list[str]
    diagram_mermaid: str | None = None
    proposed_tasks: list[PlannedTask] = Field(min_length=1)

    @model_validator(mode="after")
    def _tasks_form_dag(self) -> ArchitectureDesign:
        keys = {t.key for t in self.proposed_tasks}
        if len(keys) != len(self.proposed_tasks):
            raise ValueError("duplicate task keys")
        graph = {t.key: set(t.depends_on) for t in self.proposed_tasks}
        for k, deps in graph.items():
            if not deps <= keys:
                raise ValueError(f"task {k} depends on unknown tasks {sorted(deps - keys)}")
        # Kahn's algorithm: every node must be removable, otherwise there is a cycle.
        indegree = {k: len(v) for k, v in graph.items()}
        ready = [k for k, d in indegree.items() if d == 0]
        seen = 0
        while ready:
            node = ready.pop()
            seen += 1
            for k, deps in graph.items():
                if node in deps:
                    indegree[k] -= 1
                    if indegree[k] == 0:
                        ready.append(k)
        if seen != len(graph):
            raise ValueError("task dependencies contain a cycle")
        return self


class PlannedTask(_Schema):
    key: str = Field(pattern=_ID)
    title: str
    description: str
    acceptance_criteria_ids: list[str] = Field(min_length=1)
    depends_on: list[str] = []
    assigned_agent_key: Literal["developer"] = "developer"


ArchitectureDesign.model_rebuild()


# --------------------------------------------------------------------------- developer
class DeveloperTaskInput(_Schema):
    task: PlannedTask
    requirements: ArtifactRef
    architecture: ArtifactRef
    branch_name: str = Field(pattern=r"^aieos/[A-Za-z0-9._/-]+$")
    coding_standards: list[str] = []


class DeveloperResult(_Schema):
    status: Literal["implemented", "blocked"]
    summary: str
    patch: ArtifactRef | None = None
    tests_added: list[str] = []
    test_report: ArtifactRef | None = None
    files_changed: list[str] = []
    blocked_reason: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> DeveloperResult:
        if self.status == "implemented" and (self.patch is None or self.test_report is None):
            raise ValueError("implemented result requires a patch and a test report")
        if self.status == "blocked" and not self.blocked_reason:
            raise ValueError("blocked result requires blocked_reason")
        return self


# --------------------------------------------------------------------------- qa / security
class ReviewInput(_Schema):
    task: PlannedTask
    requirements: ArtifactRef
    patch: ArtifactRef
    author_agent_run_id: str


class Finding(_Schema):
    severity: Literal["info", "low", "medium", "high", "critical"]
    category: Literal["defect", "security", "test_gap", "standards", "acceptance"]
    title: str
    file_path: str | None = None
    line: int | None = Field(default=None, gt=0)
    evidence: str
    recommendation: str


class ReviewReport(_Schema):
    verdict: Literal["approve", "request_changes"]
    acceptance_criteria_results: dict[str, Literal["met", "not_met", "not_verifiable"]]
    findings: list[Finding]
    test_report: ArtifactRef | None = None
    security_report: ArtifactRef | None = None

    @model_validator(mode="after")
    def _verdict_matches_findings(self) -> ReviewReport:
        blocking = [f for f in self.findings if f.severity in ("high", "critical")]
        unmet = [k for k, v in self.acceptance_criteria_results.items() if v == "not_met"]
        if self.verdict == "approve" and (blocking or unmet):
            raise ValueError("cannot approve with high/critical findings or unmet acceptance criteria")
        return self


SCHEMA_REGISTRY: dict[str, type[BaseModel]] = {
    m.__name__: m
    for m in (
        SupervisorInput,
        SupervisorDecision,
        RequirementsInput,
        RequirementsSpec,
        ArchitectureInput,
        ArchitectureDesign,
        DeveloperTaskInput,
        DeveloperResult,
        ReviewInput,
        ReviewReport,
    )
}
