"""Tool catalog: the authoritative list of tools and their permission levels.

The level of a tool is a property of the tool, defined once here (loaded from
`engineering/tool-catalog.yaml`). Agents cannot raise it; contracts can only
reference tools that exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from aieos.contracts.agent_contract import TOOL_NAME_PATTERN, PermissionLevel


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=TOOL_NAME_PATTERN)
    level: PermissionLevel
    description: str
    executes_in: Literal["gateway", "sandbox", "orchestrator"]
    idempotent: bool
    # False: only the platform (agent runner or orchestrator) calls it for the run; models never see it.
    model_visible: bool = True
    # Shared/sensitive tools only: approval request expires after this many minutes.
    approval_ttl_minutes: int | None = Field(default=None, gt=0, le=7 * 24 * 60)


class ToolCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: Literal["aieos/v1"]
    tools: tuple[ToolSpec, ...]

    def get(self, name: str) -> ToolSpec | None:
        return self._index().get(name)

    def _index(self) -> dict[str, ToolSpec]:
        # Small catalog; rebuilding is cheap and keeps the model frozen/hashable.
        return {t.name: t for t in self.tools}

    def model_post_init(self, __context: object) -> None:
        names = [t.name for t in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("duplicate tool names in catalog")
        for t in self.tools:
            if t.level >= PermissionLevel.CONTROLLED_SHARED and t.approval_ttl_minutes is None:
                raise ValueError(f"{t.name}: level {int(t.level)} tools must set approval_ttl_minutes")
            if t.level >= PermissionLevel.CONTROLLED_SHARED and t.executes_in == "sandbox":
                raise ValueError(f"{t.name}: shared/sensitive tools never execute in the sandbox")


def load_catalog(path: Path) -> ToolCatalog:
    with path.open("r", encoding="utf-8") as fh:
        return ToolCatalog.model_validate(yaml.safe_load(fh))
