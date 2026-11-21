"""Separation of duties between agents' models, checked when a worker starts."""

from __future__ import annotations

from aieos.contracts.agent_contract import AgentContract
from aieos.providers.router import ModelRouter


class IndependenceError(RuntimeError):
    pass


def check_model_independence(contracts: dict[str, AgentContract], router: ModelRouter) -> None:
    """An agent that must be independent of another may share none of its resolved models
    (primary or fallback), otherwise a fallback could silently make review non-independent."""
    for key, contract in contracts.items():
        mine = set(router.resolve(contract.model))
        for other in contract.independence.must_use_different_model_from:
            shared = mine & set(router.resolve(contracts[other].model))
            if shared:
                names = sorted(f"{r.provider}/{r.model}" for r in shared)
                raise IndependenceError(f"{key} must not use the same model as {other}: {names}")
