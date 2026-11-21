from __future__ import annotations

import pytest
from pydantic import ValidationError

from aieos.contracts.io import ArchitectureDesign, DeveloperResult, RequirementsSpec, ReviewReport


def spec(**overrides):
    base = {
        "summary": "Add CSV export",
        "facts": ["Users can view reports"],
        "assumptions": [],
        "requirements": [
            {
                "id": "REQ-1",
                "statement": "Export a report as CSV",
                "priority": "must",
                "sources": [{"kind": "request", "ref": "export reports to CSV"}],
            }
        ],
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "requirement_ids": ["REQ-1"],
                "given": "a report",
                "when": "the user clicks export",
                "then": "a CSV downloads",
            }
        ],
        "open_questions": [],
    }
    base.update(overrides)
    return base


def test_valid_spec():
    assert RequirementsSpec.model_validate(spec()).has_blocking_questions is False


def test_unsourced_requirement_rejected():
    bad = spec(requirements=[{"id": "REQ-1", "statement": "Also add SSO", "priority": "must", "sources": []}])
    with pytest.raises(ValidationError):
        RequirementsSpec.model_validate(bad)


def test_must_requirement_needs_acceptance_criterion():
    with pytest.raises(ValidationError, match="without acceptance criteria"):
        RequirementsSpec.model_validate(spec(acceptance_criteria=[]))


def design(tasks):
    return {
        "overview": "o",
        "components": [{"name": "api", "responsibility": "r", "change_type": "modified"}],
        "interfaces": [],
        "data_model_changes": [],
        "decisions": [],
        "security_considerations": ["authz"],
        "risks": [],
        "proposed_tasks": tasks,
    }


def task(key, deps=()):
    return {"key": key, "title": key, "description": key, "acceptance_criteria_ids": ["AC-1"], "depends_on": list(deps)}


def test_task_graph_must_be_acyclic():
    ArchitectureDesign.model_validate(design([task("T-1"), task("T-2", ["T-1"])]))
    with pytest.raises(ValidationError, match="cycle"):
        ArchitectureDesign.model_validate(design([task("T-1", ["T-2"]), task("T-2", ["T-1"])]))


def test_reviewer_cannot_approve_with_critical_finding():
    with pytest.raises(ValidationError, match="cannot approve"):
        ReviewReport.model_validate(
            {
                "verdict": "approve",
                "acceptance_criteria_results": {"AC-1": "met"},
                "findings": [
                    {
                        "severity": "critical",
                        "category": "security",
                        "title": "SQL injection",
                        "evidence": "f-string query",
                        "recommendation": "parameterise",
                    }
                ],
            }
        )


def test_implemented_result_requires_patch_and_tests():
    with pytest.raises(ValidationError):
        DeveloperResult.model_validate({"status": "implemented", "summary": "done"})
