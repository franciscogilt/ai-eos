"""Sandbox tools (Level 2). They run in the gateway worker, which alone talks to Docker.

One sandbox per agent run, created on first use and found again by its name after a worker
restart. The command allowlist is hygiene (it keeps agents on task); the container is the
security boundary, because tests themselves execute arbitrary repository code.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from aieos.artifacts.store import ArtifactStore
from aieos.gateway.handlers import Handler, ToolContext, ToolFailedError, ToolInputError
from aieos.integrations.github.patch import PatchRejectedError, validate_patch
from aieos.sandbox.docker import GIT_IDENTITY, DockerSandboxManager, SandboxError, SandboxKey, UnsafePathError

DEFAULT_ALLOWED_COMMANDS = frozenset(
    {
        "python",
        "python3",
        "pytest",
        "ruff",
        "mypy",
        "git",
        "ls",
        "cat",
        "head",
        "tail",
        "grep",
        "find",
        "wc",
        "diff",
        "make",
        "echo",
        "true",
    }
)
MAX_FILE_BYTES = 1024 * 1024
_SELECTOR = re.compile(r"^[A-Za-z0-9_./:\[\]\-=, ]{1,300}$")


# Secret patterns for added lines. Deliberately few and specific: a hit blocks approval, so noise is costly.
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    "aws-access-key-id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "anthropic-key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    "openai-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
}


def scan_added_lines(patch: str) -> list[dict[str, Any]]:
    """Secret findings in the lines a patch adds. The matched value is never returned."""
    findings: list[dict[str, Any]] = []
    path, line_no = None, 0
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            line_no = int(m.group(1)) - 1 if m else 0
        elif line.startswith("+") and path:
            line_no += 1
            for rule, pattern in SECRET_PATTERNS.items():
                if pattern.search(line):
                    findings.append(
                        {"tool": "secrets", "rule": rule, "severity": "high", "file": path, "line": line_no}
                    )
        elif not line.startswith("-"):
            line_no += 1
    return findings


async def apply_patch(manager: DockerSandboxManager, key: SandboxKey, patch: bytes, *, what: str) -> None:
    """Validate and apply a platform patch artifact to the sandbox working tree (no commit)."""
    try:
        validate_patch(patch)
    except PatchRejectedError as exc:
        raise ToolInputError(f"{what} rejected: {exc}") from None
    applied = await manager.run(
        key, ["git", "-c", "core.attributesFile=/dev/null", "apply", "--whitespace=nowarn", "-"], stdin=patch
    )
    if applied.exit_code != 0:
        raise ToolInputError(f"{what} does not apply: {applied.stderr.decode(errors='replace')[:300]}")


def _key(ctx: ToolContext) -> SandboxKey:
    return SandboxKey(org_id=ctx.org_id, workflow_id=ctx.workflow_id, agent_run_id=ctx.agent_run_id)


def _argv(command: Any, allowed: frozenset[str]) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise ToolInputError("command (non-empty string) is required")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ToolInputError(f"cannot parse command: {exc}") from None
    program = argv[0]
    if "/" in program or program not in allowed:
        raise ToolInputError(f"{program!r} is not an allowed command; allowed: {', '.join(sorted(allowed))}")
    return argv


def _exec_result(r: Any) -> dict[str, Any]:
    return {
        "exit_code": r.exit_code,
        "stdout": r.stdout,
        "stderr": r.stderr,
        "timed_out": r.timed_out,
        "duration_ms": r.duration_ms,
        "truncated": r.truncated,
    }


Snapshot = Callable[[ToolContext], Awaitable[bytes]]


def sandbox_handlers(
    manager: DockerSandboxManager,
    store: ArtifactStore,
    *,
    allowed_commands: frozenset[str] = DEFAULT_ALLOWED_COMMANDS,
    snapshot: Snapshot | None = None,
) -> dict[str, Handler]:
    """`snapshot` (B13) seeds a new sandbox with the workflow's repository at its base commit;
    without it a sandbox starts empty."""

    async def ensure(ctx: ToolContext) -> None:
        source = snapshot
        await manager.ensure(_key(ctx), snapshot=(lambda: source(ctx)) if source is not None else None)

    async def write_file(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        path, content = args.get("path"), args.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise ToolInputError("path (str) and content (str) are required")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ToolInputError(f"file larger than {MAX_FILE_BYTES} bytes")
        await ensure(ctx)
        try:
            written = await manager.write_file(_key(ctx), path, data)
        except UnsafePathError as exc:
            raise ToolInputError(str(exc)) from None
        return {"path": written, "bytes": len(data)}

    async def run_command(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        argv = _argv(args.get("command"), allowed_commands)
        await ensure(ctx)
        try:
            result = await manager.exec(_key(ctx), argv, workdir=args.get("working_dir") or None)
        except UnsafePathError as exc:
            raise ToolInputError(str(exc)) from None
        return _exec_result(result)

    async def run_tests(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        selector = args.get("selector")
        argv = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        if selector:
            if not isinstance(selector, str) or not _SELECTOR.match(selector):
                raise ToolInputError("selector may contain only test paths, node ids and -k/-m expressions")
            argv += shlex.split(selector)
        await ensure(ctx)
        result = _exec_result(await manager.exec(_key(ctx), argv))
        result["passed"] = result["exit_code"] == 0 and not result["timed_out"]
        return result

    async def export_patch(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        await ensure(ctx)
        patch, files = await manager.export_patch(_key(ctx))
        if not patch:
            return {"empty": True, "files_changed": []}
        row = await store.put(ctx.org_id, patch, kind="patch", content_type="text/x-diff", workflow_id=ctx.workflow_id)
        try:
            validate_patch(patch)
            violations: list[str] = []
        except PatchRejectedError as exc:  # reported now, so the agent can fix it before review
            violations = [str(exc)]
        return {
            "empty": False,
            "artifact_id": str(row.id),
            "sha256": row.sha256,
            "size_bytes": row.size_bytes,
            "files_changed": files,
            "policy_violations": violations,
            "summary": str(args.get("summary", ""))[:500],
        }

    async def prepare(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        """Fresh sandbox = base snapshot + committed `base_patches` + uncommitted `apply_patches`."""

        def ids(name: str) -> list[UUID]:
            raw = args.get(name) or []
            if not isinstance(raw, list) or len(raw) > 100:
                raise ToolInputError(f"{name} must be a list of at most 100 artifact ids")
            try:
                return [UUID(str(x)) for x in raw]
            except ValueError:
                raise ToolInputError(f"{name} must contain artifact ids") from None

        base_ids, apply_ids = ids("base_patches"), ids("apply_patches")
        patches: dict[UUID, bytes] = {}
        for artifact_id in [*base_ids, *apply_ids]:
            row, data = await store.get(ctx.org_id, artifact_id)
            if row.kind != "patch" or row.workflow_id != ctx.workflow_id:
                raise ToolInputError(f"{artifact_id} is not a patch artifact of this workflow")
            patches[artifact_id] = data
        key = _key(ctx)
        try:
            await manager.destroy(key)  # never build on a leftover from an interrupted attempt
            await ensure(ctx)
            for artifact_id in base_ids:
                await apply_patch(manager, key, patches[artifact_id], what=f"approved patch {artifact_id}")
            if base_ids:
                for argv in (
                    ["git", "add", "-A"],
                    ["git", *GIT_IDENTITY, "commit", "-q", "-m", "aieos approved patches"],
                ):
                    r = await manager.run(key, argv)
                    if r.exit_code != 0:
                        raise ToolFailedError(
                            f"could not commit approved patches: {r.stderr.decode(errors='replace')[:300]}"
                        )
            for artifact_id in apply_ids:
                await apply_patch(manager, key, patches[artifact_id], what=f"patch {artifact_id}")
        except SandboxError as exc:
            raise ToolFailedError(f"sandbox preparation failed: {exc}") from None
        return {"base_patches": [str(i) for i in base_ids], "applied_patches": [str(i) for i in apply_ids]}

    async def run_security_scan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        """SAST (bandit) on changed Python files and a secret scan of the added lines.
        Dependency audits need network access, which sandboxes do not have; they run in CI."""
        await ensure(ctx)
        key = _key(ctx)
        patch, files = await manager.export_patch(key)
        text = patch.decode("utf-8", errors="replace")
        findings = scan_added_lines(text)
        added = {line[6:] for line in text.splitlines() if line.startswith("+++ b/")}
        python_files = sorted(f for f in files if f.endswith(".py") and f in added)
        scanners = ["secrets"]
        if python_files:
            r = await manager.run(key, ["bandit", "-q", "-f", "json", "--", *python_files], timeout=300)
            try:
                report = json.loads(r.stdout or b"{}")
            except ValueError:
                raise ToolFailedError(f"bandit failed: {r.stderr.decode(errors='replace')[:300]}") from None
            scanners.append("bandit")
            for res in report.get("results", []):
                findings.append(
                    {
                        "tool": "bandit",
                        "rule": f"{res.get('test_id')} {res.get('test_name')}",
                        "severity": str(res.get("issue_severity", "LOW")).lower(),
                        "confidence": str(res.get("issue_confidence", "")).lower(),
                        "file": res.get("filename"),
                        "line": res.get("line_number"),
                        "message": str(res.get("issue_text", ""))[:300],
                    }
                )
        stored = await store.put(
            ctx.org_id,
            json.dumps({"scanners": scanners, "findings": findings}, indent=2).encode(),
            kind="security_report",
            content_type="application/json",
            workflow_id=ctx.workflow_id,
        )
        return {
            "scanners": scanners,
            "files_scanned": python_files,
            "findings": findings[:100],
            "truncated": len(findings) > 100,
            "report_artifact_id": str(stored.id),
            "report_sha256": stored.sha256,
            "not_run": ["dependency audit (needs network; runs in CI)"],
        }

    return {
        "sandbox.prepare": prepare,
        "sandbox.write_file": write_file,
        "sandbox.run_command": run_command,
        "sandbox.run_tests": run_tests,
        "sandbox.run_security_scan": run_security_scan,
        "sandbox.export_patch": export_patch,
    }
