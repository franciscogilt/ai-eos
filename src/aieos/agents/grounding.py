"""Checks that do not trust the model: every claim must trace back to an input.

Requirements: each source must actually exist. A `request` quote must appear in the request,
a `clarification` id must be one that was answered, and a `repository` path must have been
read during the run. "Never invent requirements" becomes a check on every output.

Architecture: tasks may only reference acceptance criteria that exist, and every criterion
must be covered by at least one task.
"""

from __future__ import annotations

from aieos.contracts.io import ArchitectureDesign, RequirementsSpec


def _norm(text: str) -> str:
    return " ".join(text.lower().split()).strip(" \"'\u201c\u201d\u2018\u2019")


def requirement_grounding_violations(
    spec: RequirementsSpec, *, request: str, clarification_ids: set[str], repository_paths: set[str]
) -> list[str]:
    haystack = _norm(request)
    violations: list[str] = []
    for req in spec.requirements:
        for src in req.sources:
            if src.kind == "request":
                quote = _norm(src.ref)
                if len(quote) < 3 or quote not in haystack:
                    violations.append(f"{req.id}: quoted text is not in the request: {src.ref[:80]!r}")
            elif src.kind == "clarification":
                if src.ref not in clarification_ids:
                    violations.append(f"{req.id}: no answered clarification with id {src.ref!r}")
            elif src.ref not in repository_paths:
                violations.append(f"{req.id}: repository file {src.ref!r} was not read in this run")
    return violations


def architecture_violations(design: ArchitectureDesign, spec: RequirementsSpec) -> list[str]:
    known = {ac.id for ac in spec.acceptance_criteria}
    violations: list[str] = []
    covered: set[str] = set()
    for task in design.proposed_tasks:
        unknown = set(task.acceptance_criteria_ids) - known
        if unknown:
            violations.append(f"{task.key}: unknown acceptance criteria {sorted(unknown)}")
        covered |= set(task.acceptance_criteria_ids)
    missing = known - covered
    if missing:
        violations.append(f"acceptance criteria not covered by any task: {sorted(missing)}")
    return violations
