"""JSON schemas of tool arguments, as shown to models (strict mode: every property required,
optional values are nullable). Handlers validate again; these schemas are not a security boundary.
Orchestrator tools are absent: models never call them."""

from __future__ import annotations

from typing import Any


def _obj(**props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_STR = {"type": "string"}
_OPT_STR = {"type": ["string", "null"]}

TOOL_ARG_SCHEMAS: dict[str, dict[str, Any]] = {
    "repo.list_tree": _obj(path={**_OPT_STR, "description": "Directory relative to the repo root; null = root"}),
    "repo.read_file": _obj(path={**_STR, "description": "File path relative to the repository root"}),
    "repo.search": _obj(
        query={**_STR, "description": "Text or regular expression"},
        path={**_OPT_STR, "description": "Limit the search to this directory"},
    ),
    "docs.fetch": _obj(url={**_STR, "description": "https URL on an allowlisted documentation domain"}),
    "artifacts.read": _obj(artifact_id={**_STR, "description": "Artifact UUID from this workflow"}),
    "artifacts.write": _obj(
        kind={**_STR, "description": "Artifact kind, e.g. requirements_spec"}, content=_STR, content_type=_OPT_STR
    ),
    "sandbox.write_file": _obj(path=_STR, content=_STR),
    "sandbox.run_command": _obj(command={**_STR, "description": "Allowlisted command line"}, working_dir=_OPT_STR),
    "sandbox.run_tests": _obj(selector={**_OPT_STR, "description": "Optional test selector; null = full suite"}),
    "sandbox.run_security_scan": _obj(scanners={"type": ["array", "null"], "items": _STR}),
    "sandbox.export_patch": _obj(summary=_STR),
    "github.push_branch": _obj(patch_artifact_id=_STR, branch=_STR),
    "github.create_pull_request": _obj(head=_STR, title=_STR, body=_STR),
    "github.comment": _obj(pull_request=_STR, body=_STR),
    "github.merge_pull_request": _obj(pull_request=_STR),
    "staging.deploy": _obj(commit_sha=_STR),
    "production.deploy": _obj(commit_sha=_STR),
    "database.destructive_migration": _obj(migration=_STR),
    "secrets.manage": _obj(action=_STR, secret_name=_STR),
    "iam.modify": _obj(change=_STR),
}
