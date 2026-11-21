from __future__ import annotations

from pathlib import Path

import pytest

from aieos.gateway.catalog import ToolCatalog, load_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINEERING = REPO_ROOT / "engineering"


@pytest.fixture(scope="session")
def catalog() -> ToolCatalog:
    return load_catalog(ENGINEERING / "tool-catalog.yaml")


@pytest.fixture(scope="session")
def definitions_dir() -> Path:
    return ENGINEERING / "agent-definitions"


@pytest.fixture(scope="session")
def prompts_dir() -> Path:
    return ENGINEERING / "prompts"
