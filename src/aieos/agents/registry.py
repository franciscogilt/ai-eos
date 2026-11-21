"""Load and cross-validate agent contracts against the tool catalog and prompt files.

Used by tests/CI (every contract in the repo must load) and by the publish
command that writes `agent_versions` rows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from aieos.contracts.agent_contract import AgentContract
from aieos.contracts.io import SCHEMA_REGISTRY
from aieos.gateway.catalog import ToolCatalog

SHARED_PREAMBLE = "_shared_untrusted_input.md"


class ContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LoadedContract:
    contract: AgentContract
    contract_sha256: str
    prompt_sha256: str
    prompt_text: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_contract(path: Path, *, prompts_dir: Path, catalog: ToolCatalog) -> LoadedContract:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    contract = AgentContract.model_validate(raw)

    if path.stem != contract.key.replace("_", "-") and path.stem != contract.key:
        raise ContractError(f"{path.name}: file name must match key {contract.key!r}")

    for schema_name in (contract.input_schema, contract.output_schema):
        if schema_name not in SCHEMA_REGISTRY:
            raise ContractError(f"{contract.key}: unknown schema {schema_name!r}")

    for tool_name in contract.tools:
        tool = catalog.get(tool_name)
        if tool is None:
            raise ContractError(f"{contract.key}: tool {tool_name!r} is not in the catalog")
        if tool.level > contract.max_permission_level:
            raise ContractError(
                f"{contract.key}: tool {tool_name} (level {int(tool.level)}) exceeds "
                f"max_permission_level {int(contract.max_permission_level)}"
            )

    shared_path = prompts_dir / SHARED_PREAMBLE
    if not shared_path.is_file():
        raise ContractError(f"shared prompt preamble {SHARED_PREAMBLE} not found")
    prompt_path = (prompts_dir / contract.instructions_path).resolve()
    if prompts_dir.resolve() not in prompt_path.parents:
        raise ContractError(f"{contract.key}: instructions_path escapes the prompts directory")
    if not prompt_path.is_file():
        raise ContractError(f"{contract.key}: prompt file {contract.instructions_path} not found")
    # The shared untrusted-content rules are part of every prompt, so they are part of its hash:
    # editing them changes every agent's prompt_sha256 and requires new versions.
    prompt_bytes = shared_path.read_bytes().rstrip() + b"\n\n" + prompt_path.read_bytes()

    canonical = json.dumps(contract.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return LoadedContract(
        contract=contract,
        contract_sha256=_sha256(canonical.encode("utf-8")),
        prompt_sha256=_sha256(prompt_bytes),
        prompt_text=prompt_bytes.decode("utf-8"),
    )


def load_all(definitions_dir: Path, *, prompts_dir: Path, catalog: ToolCatalog) -> dict[str, LoadedContract]:
    loaded: dict[str, LoadedContract] = {}
    for path in sorted(definitions_dir.glob("*.yaml")):
        item = load_contract(path, prompts_dir=prompts_dir, catalog=catalog)
        if item.contract.key in loaded:
            raise ContractError(f"duplicate agent key {item.contract.key}")
        loaded[item.contract.key] = item

    # Separation of duties across contracts.
    for item in loaded.values():
        for other in item.contract.independence.must_use_different_model_from:
            if other not in loaded:
                raise ContractError(f"{item.contract.key}: independence refers to unknown agent {other}")
    return loaded
