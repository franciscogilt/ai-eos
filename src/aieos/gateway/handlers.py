"""Tool handlers: the code that actually performs an authorized tool call.

Handlers run only after `ToolGateway` has authorized the call. GitHub and sandbox
handlers arrive in B13; until then those tools fail with "no handler", which is the
safe outcome.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aieos.artifacts.store import ArtifactStore
from aieos.gateway.catalog import ToolSpec


class ToolInputError(ValueError):
    """The arguments are invalid. Not retried."""


class ToolTransientError(RuntimeError):
    """A temporary failure (network, rate limit). Retried only for idempotent tools."""


@dataclass(frozen=True, slots=True)
class ToolContext:
    org_id: UUID
    workflow_id: UUID
    agent_run_id: UUID
    agent_key: str
    tool: ToolSpec


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]

MAX_INLINE_TEXT = 256 * 1024


def artifact_handlers(store: ArtifactStore) -> dict[str, Handler]:
    async def read(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        try:
            artifact_id = UUID(str(args["artifact_id"]))
        except (KeyError, ValueError):
            raise ToolInputError("artifact_id (uuid) is required") from None
        data = await store.get_bytes(ctx.org_id, artifact_id)  # RLS-scoped, integrity-checked
        if len(data) > MAX_INLINE_TEXT:
            return {
                "artifact_id": str(artifact_id),
                "truncated": True,
                "size_bytes": len(data),
                "url": await store.presign_download(ctx.org_id, artifact_id),
            }
        # Untrusted content: returned as data, wrapped by the agent factory before reaching a model.
        return {"artifact_id": str(artifact_id), "text": data.decode("utf-8", errors="replace")}

    async def write(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        content, kind = args.get("content"), args.get("kind")
        if not isinstance(content, str) or not isinstance(kind, str):
            raise ToolInputError("content (str) and kind (str) are required")
        content_type = str(args.get("content_type") or "text/plain; charset=utf-8")
        try:
            row = await store.put(
                ctx.org_id, content.encode("utf-8"), kind=kind, content_type=content_type, workflow_id=ctx.workflow_id
            )
        except ValueError as exc:
            raise ToolInputError(str(exc)) from None
        return {"artifact_id": str(row.id), "sha256": row.sha256, "size_bytes": row.size_bytes}

    return {"artifacts.read": read, "artifacts.write": write}
