from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from aieos.agents.registry import ContractError, load_all, load_contract
from aieos.contracts.agent_contract import AgentContract, PermissionLevel

MVP_AGENTS = {"supervisor", "requirements", "architecture", "developer", "qa_security"}


def test_all_mvp_contracts_load(definitions_dir, prompts_dir, catalog):
    loaded = load_all(definitions_dir, prompts_dir=prompts_dir, catalog=catalog)
    assert set(loaded) == MVP_AGENTS
    for item in loaded.values():
        assert len(item.contract_sha256) == 64
        assert item.prompt_text.strip()


def test_only_supervisor_reaches_level3(definitions_dir, prompts_dir, catalog):
    loaded = load_all(definitions_dir, prompts_dir=prompts_dir, catalog=catalog)
    for key, item in loaded.items():
        levels = {catalog.get(t).level for t in item.contract.tools}
        assert PermissionLevel.SENSITIVE not in levels, key
        if key != "supervisor":
            assert max(levels) <= PermissionLevel.ISOLATED_EXECUTION, key


def test_developer_cannot_push_or_merge(definitions_dir, prompts_dir, catalog):
    dev = load_all(definitions_dir, prompts_dir=prompts_dir, catalog=catalog)["developer"].contract
    assert not {t for t in dev.tools if t.startswith("github.")}


def test_reviewer_is_independent_of_developer(definitions_dir, prompts_dir, catalog):
    loaded = load_all(definitions_dir, prompts_dir=prompts_dir, catalog=catalog)
    qa = loaded["qa_security"].contract
    assert qa.independence.may_review_own_output is False
    assert "developer" in qa.independence.must_use_different_model_from
    assert qa.model.primary.provider != loaded["developer"].contract.model.primary.provider


def _write_variant(tmp_path: Path, definitions_dir: Path, prompts_dir: Path, **changes) -> Path:
    raw = yaml.safe_load((definitions_dir / "developer.yaml").read_text())
    raw.update(changes)
    out = tmp_path / "developer.yaml"
    out.write_text(yaml.safe_dump(raw))
    return out


def test_contract_rejects_tool_above_max_level(tmp_path, definitions_dir, prompts_dir, catalog):
    path = _write_variant(tmp_path, definitions_dir, prompts_dir, tools=["repo.read_file", "production.deploy"])
    with pytest.raises(ContractError, match="exceeds max_permission_level"):
        load_contract(path, prompts_dir=prompts_dir, catalog=catalog)


def test_contract_rejects_unknown_tool(tmp_path, definitions_dir, prompts_dir, catalog):
    path = _write_variant(tmp_path, definitions_dir, prompts_dir, tools=["shell.exec_anything"])
    with pytest.raises(ContractError, match="not in the catalog"):
        load_contract(path, prompts_dir=prompts_dir, catalog=catalog)


def test_contract_rejects_prompt_path_traversal(definitions_dir):
    raw = yaml.safe_load((definitions_dir / "developer.yaml").read_text())
    raw["instructions_path"] = "../../etc/passwd"
    with pytest.raises(ValidationError):
        AgentContract.model_validate(raw)


def test_contract_rejects_unknown_fields(definitions_dir):
    raw = yaml.safe_load((definitions_dir / "developer.yaml").read_text())
    raw["unrestricted"] = True
    with pytest.raises(ValidationError):
        AgentContract.model_validate(raw)
