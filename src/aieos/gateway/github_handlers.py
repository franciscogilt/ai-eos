"""GitHub tools (B13): repository snapshots, read-only repo tools, the validated push path and PRs.

Trust boundaries:
* The App's private key and installation tokens exist only in this worker (integrations.github).
  No token ever enters a sandbox; sandboxes have no network at all.
* Every workflow is pinned to one base commit (`workflows.base_commit_sha`, set once). Sandboxes are
  seeded with the repository at that commit, and patches are pushed on top of it.
* `repo.*` tools read the snapshot in this process's memory; nothing is extracted to disk here.
* `github.push_branch` pushes exactly the reviewed patch artifact:
    1. the patch must be a `patch` artifact of the same workflow, and pass `validate_patch`;
    2. it is applied in a fresh, network-less sandbox seeded with the base snapshot;
    3. the base content of every touched file is hashed (`git hash-object --no-filters`) and must
       equal the blob in GitHub's base tree, so the snapshot cannot differ from the real repository;
    4. after applying, `git status` must list exactly the files the validator found;
    5. the resulting files are uploaded through the Git Data API as one commit on the base commit,
       and the branch is created or moved. Only branches `aieos/<workflow id>[/...]` are accepted.
"""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from aieos.artifacts.store import ArtifactStore
from aieos.core.logging import get_logger
from aieos.gateway.handlers import Handler, ToolContext, ToolFailedError, ToolInputError, ToolTransientError
from aieos.gateway.sandbox_handlers import apply_patch
from aieos.integrations.github.app import GitHubApp, GitHubError, GitHubRepo, RepoRef
from aieos.integrations.github.patch import FileChange, PatchRejectedError, net_changes, validate_patch
from aieos.persistence.db import Database
from aieos.sandbox.docker import DockerSandboxManager, SandboxError, SandboxKey

log = get_logger(__name__)

_BRANCH_PART = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}$")
_PUSH_NAMESPACE = uuid.UUID("6f1c1d1e-4b0a-4f57-9a43-5b2f3b8f2a11")
MAX_SNAPSHOT_BYTES = 200 * 1024 * 1024  # compressed download
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024  # matches the sandbox workspace size
MAX_READ_BYTES = 256 * 1024
MAX_PUSH_BYTES = 20 * 1024 * 1024
MAX_SEARCH_RESULTS = 100
MAX_TREE_ENTRIES = 1000


@dataclass(frozen=True, slots=True)
class WorkflowRepo:
    repo: RepoRef
    default_branch: str
    base_sha: str
    title: str


def check_branch(branch: Any, workflow_id: UUID) -> str:
    """Only `aieos/<workflow id>` or `aieos/<workflow id>/<name>...`: a workflow can never touch
    another workflow's branches, let alone a human branch."""
    if not isinstance(branch, str):
        raise ToolInputError("branch (string) is required")
    prefix = f"aieos/{workflow_id}"
    if branch != prefix and not branch.startswith(prefix + "/"):
        raise ToolInputError(f"branch must be {prefix} or start with {prefix}/")
    rest = branch[len(prefix) + 1 :].split("/") if branch != prefix else []
    if len(branch) > 200 or any(not _BRANCH_PART.match(p) or p.endswith((".lock", ".")) for p in rest):
        raise ToolInputError("branch segments may use letters, digits and ._- only")
    if ".." in branch:
        raise ToolInputError("branch may not contain '..'")
    return branch


def git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(content) + content, usedforsecurity=False).hexdigest()


class _Snapshot:
    """Regular files of a GitHub tarball, keyed by repository-relative path."""

    def __init__(self, tar_gz: bytes) -> None:
        self.raw = tar_gz
        self.files: dict[str, bytes] = {}
        unpacked = 0
        # Streaming read with a running total: a decompression bomb stops at the limit.
        with tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r|gz") as tar:
            for member in tar:
                _, _, rel = member.name.partition("/")
                if not (member.isreg() and rel):
                    continue
                unpacked += member.size
                if unpacked > MAX_UNPACKED_BYTES:
                    raise ValueError(f"repository is larger than {MAX_UNPACKED_BYTES} bytes unpacked")
                fh = tar.extractfile(member)
                if fh is not None:
                    self.files[rel] = fh.read()
        self.size = len(tar_gz) + unpacked

    def listing(self, directory: str) -> list[dict[str, Any]]:
        prefix = f"{directory.strip('/')}/" if directory.strip("/") else ""
        entries: dict[str, dict[str, Any]] = {}
        for path, data in self.files.items():
            if not path.startswith(prefix):
                continue
            head, sep, _ = path[len(prefix) :].partition("/")
            if sep:
                entries.setdefault(head, {"name": head, "type": "dir"})
            else:
                entries[head] = {"name": head, "type": "file", "size": len(data)}
        return sorted(entries.values(), key=lambda e: (e["type"] != "dir", e["name"]))


class GitHubTools:
    def __init__(
        self,
        db: Database,
        app: GitHubApp,
        store: ArtifactStore,
        sandboxes: DockerSandboxManager | None,
        *,
        cache_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        self._db = db
        self._app = app
        self._store = store
        self._sandboxes = sandboxes
        self._cache: OrderedDict[tuple[str, str], _Snapshot] = OrderedDict()
        self._cache_bytes = cache_bytes

    # ------------------------------------------------------------------ workflow -> repository
    async def workflow_repo(self, org_id: UUID, workflow_id: UUID) -> WorkflowRepo:
        query = text(
            """SELECT w.title, w.base_commit_sha, r.owner_login, r.name, r.default_branch,
                      r.github_installation_id, r.status
               FROM workflows w JOIN repositories r ON r.org_id = w.org_id AND r.id = w.repository_id
               WHERE w.id = :wf"""
        )
        async with self._db.tenant(org_id) as conn:
            row = (await conn.execute(query, {"wf": workflow_id})).first()
        if row is None:
            raise ToolInputError("workflow not found")
        if row.status != "active":
            raise ToolFailedError("the repository is disconnected")
        ref = RepoRef(owner=row.owner_login, name=row.name, installation_id=int(row.github_installation_id))
        base = row.base_commit_sha
        if base is None:
            head = await self._github(self._app.repo(ref).branch_head(row.default_branch))
            async with self._db.tenant(org_id) as conn:  # first writer wins; the trigger forbids changes
                await conn.execute(
                    text("UPDATE workflows SET base_commit_sha = :sha WHERE id = :wf AND base_commit_sha IS NULL"),
                    {"sha": head, "wf": workflow_id},
                )
                base = (
                    await conn.execute(
                        text("SELECT base_commit_sha FROM workflows WHERE id = :wf"), {"wf": workflow_id}
                    )
                ).scalar_one()
            log.info("workflow.base_commit_pinned", workflow_id=str(workflow_id), base_commit_sha=base)
        return WorkflowRepo(ref, row.default_branch, str(base), row.title)

    @staticmethod
    async def _github(call: Any) -> Any:
        try:
            return await call
        except GitHubError as exc:
            if exc.transient:
                raise ToolTransientError(str(exc)) from None
            raise ToolFailedError(str(exc)) from None

    # ------------------------------------------------------------------ snapshots
    async def _snapshot(self, wr: WorkflowRepo) -> _Snapshot:
        key = (wr.repo.full_name, wr.base_sha)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        raw = await self._github(self._app.repo(wr.repo).tarball(wr.base_sha, max_bytes=MAX_SNAPSHOT_BYTES))
        try:
            snap = _Snapshot(raw)
        except (OSError, EOFError, ValueError, tarfile.TarError) as exc:
            raise ToolFailedError(f"repository snapshot could not be used: {exc}") from None
        self._cache[key] = snap
        while sum(s.size for s in self._cache.values()) > self._cache_bytes and len(self._cache) > 1:
            self._cache.popitem(last=False)
        return snap

    async def snapshot_for(self, ctx: ToolContext) -> bytes:
        """Seed for a new sandbox (passed to sandbox_handlers)."""
        return (await self._snapshot(await self.workflow_repo(ctx.org_id, ctx.workflow_id))).raw

    # ------------------------------------------------------------------ repo.* (read-only, Level 1)
    async def _snap(self, ctx: ToolContext) -> _Snapshot:
        return await self._snapshot(await self.workflow_repo(ctx.org_id, ctx.workflow_id))

    async def list_tree(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        directory = args.get("path") or ""
        if not isinstance(directory, str):
            raise ToolInputError("path must be a string")
        entries = (await self._snap(ctx)).listing(directory)
        if not entries and directory.strip("/"):
            raise ToolInputError(f"no such directory: {directory}")
        return {"path": directory, "entries": entries[:MAX_TREE_ENTRIES], "truncated": len(entries) > MAX_TREE_ENTRIES}

    async def read_file(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path")
        if not isinstance(path, str) or not path:
            raise ToolInputError("path (string) is required")
        data = (await self._snap(ctx)).files.get(path.strip("/"))
        if data is None:
            raise ToolInputError(f"no such file: {path}")
        if b"\x00" in data[:8192]:
            return {"path": path, "binary": True, "size": len(data)}
        return {
            "path": path,
            "text": data[:MAX_READ_BYTES].decode("utf-8", errors="replace"),
            "truncated": len(data) > MAX_READ_BYTES,
            "size": len(data),
        }

    async def search(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        """Case-insensitive substring search. Deliberately not a regex: patterns come from a model."""
        query, under = args.get("query"), (args.get("path") or "").strip("/")
        if not isinstance(query, str) or not 2 <= len(query) <= 200:
            raise ToolInputError("query must be 2 to 200 characters")
        needle = query.lower()
        matches: list[dict[str, Any]] = []
        for path, data in sorted((await self._snap(ctx)).files.items()):
            if under and not path.startswith(under + "/"):
                continue
            if b"\x00" in data[:8192]:
                continue
            for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if needle in line.lower():
                    matches.append({"path": path, "line": number, "text": line.strip()[:200]})
                    if len(matches) >= MAX_SEARCH_RESULTS:
                        return {"query": query, "matches": matches, "truncated": True}
        return {"query": query, "matches": matches, "truncated": False}

    # ------------------------------------------------------------------ github.push_branch (Level 3)
    async def _base_entry(self, repo: GitHubRepo, root_tree: str, path: str, cache: dict[str, Any]) -> Any:
        """The base tree entry for `path` ({mode, type, sha}) or None, walking one directory at a time."""
        tree_sha = root_tree
        parts = path.split("/")
        for depth, part in enumerate(parts):
            if tree_sha not in cache:
                cache[tree_sha] = {e["path"]: e for e in await self._github(repo.tree(tree_sha))}
            entry = cache[tree_sha].get(part)
            if entry is None:
                return None
            if depth == len(parts) - 1:
                return entry
            if entry["type"] != "tree":
                return None
            tree_sha = entry["sha"]
        return None

    async def push_branch(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        """Push the approved patches (in order; each made on top of the previous ones) as one commit."""
        if self._sandboxes is None:
            raise ToolFailedError("pushing needs the sandbox backend (SANDBOX_BACKEND=docker)")
        branch = check_branch(args.get("branch"), ctx.workflow_id)
        raw_ids = args.get("patch_artifact_ids")
        if raw_ids is None and args.get("patch_artifact_id") is not None:
            raw_ids = [args["patch_artifact_id"]]
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 100:
            raise ToolInputError("patch_artifact_ids must list 1 to 100 patch artifacts, in order")
        try:
            artifact_ids = [UUID(str(x)) for x in raw_ids]
        except ValueError:
            raise ToolInputError("patch_artifact_ids must contain artifact ids") from None
        patches: list[bytes] = []
        digests: list[str] = []
        per_patch: list[list[FileChange]] = []
        for artifact_id in artifact_ids:
            row, patch = await self._store.get(ctx.org_id, artifact_id)
            if row.kind != "patch" or row.workflow_id != ctx.workflow_id:
                raise ToolInputError(f"{artifact_id} is not a patch artifact of this workflow")
            try:
                per_patch.append(validate_patch(patch))
            except PatchRejectedError as exc:
                raise ToolInputError(f"patch {artifact_id} rejected: {exc}") from None
            patches.append(patch)
            digests.append(row.sha256)
        try:
            changes = net_changes(per_patch)
        except PatchRejectedError as exc:
            raise ToolInputError(f"patches do not stack: {exc}") from None
        if not changes:
            raise ToolInputError("the patches cancel out; nothing to push")

        wr = await self.workflow_repo(ctx.org_id, ctx.workflow_id)
        repo = self._app.repo(wr.repo)
        snap = await self._snapshot(wr)
        files = await self._apply_in_sandbox(ctx, artifact_ids, snap, patches, changes, repo, wr.base_sha)

        base_tree = await self._github(repo.commit_tree(wr.base_sha))
        cache: dict[str, Any] = {}
        entries: list[dict[str, Any]] = []
        blobs: dict[str, str] = {}
        for c in changes:
            base = await self._base_entry(repo, base_tree, c.path, cache)
            if c.kind == "delete":
                entries.append({"path": c.path, "mode": base["mode"], "type": "blob", "sha": None})
                continue
            content = files[c.path]
            digest = hashlib.sha256(content).hexdigest()
            if digest not in blobs:
                blobs[digest] = await self._github(repo.create_blob(content))
            mode = c.mode or (base["mode"] if base else "100644")
            entries.append({"path": c.path, "mode": mode, "type": "blob", "sha": blobs[digest]})
        tree = await self._github(repo.create_tree(base_tree, entries))
        provenance = "\n".join(f"Patch artifact {a} (sha256 {d})." for a, d in zip(artifact_ids, digests, strict=True))
        message = f"{wr.title[:72]}\n\nPushed by AI-EOS for workflow {ctx.workflow_id}.\n{provenance}"
        commit = await self._github(repo.create_commit(message, tree, wr.base_sha))
        await self._github(repo.set_branch(branch, commit))
        log.info("github.pushed", workflow_id=str(ctx.workflow_id), branch=branch, commit=commit, files=len(changes))
        return {
            "branch": branch,
            "commit_sha": commit,
            "base_commit_sha": wr.base_sha,
            "patches": [str(a) for a in artifact_ids],
            "files_changed": [{"path": c.path, "change": c.kind} for c in changes],
        }

    async def _apply_in_sandbox(
        self,
        ctx: ToolContext,
        artifact_ids: list[UUID],
        snap: _Snapshot,
        patches: list[bytes],
        changes: list[FileChange],
        repo: GitHubRepo,
        base_sha: str,
    ) -> dict[str, bytes]:
        assert self._sandboxes is not None
        sandboxes = self._sandboxes
        key = SandboxKey(
            org_id=ctx.org_id,
            workflow_id=ctx.workflow_id,
            agent_run_id=uuid.uuid5(_PUSH_NAMESPACE, f"{ctx.agent_run_id}:{','.join(map(str, artifact_ids))}"),
        )

        async def seed() -> bytes:
            return snap.raw

        try:
            await sandboxes.destroy(key)  # a leftover from an interrupted attempt is never reused
            await sandboxes.ensure(key, snapshot=seed)
            await self._verify_base(sandboxes, key, changes, repo, base_sha)
            for artifact_id, patch in zip(artifact_ids, patches, strict=True):
                try:
                    await apply_patch(sandboxes, key, patch, what=f"patch {artifact_id}")
                except ToolInputError as exc:
                    raise ToolInputError(f"{exc} (to the base commit plus the earlier patches)") from None
            status = await sandboxes.run(
                key,
                [
                    "git",
                    "-c",
                    "core.quotePath=false",
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                    "--no-renames",
                ],
            )
            found: dict[str, str] = {}
            for item in status.stdout.decode("utf-8", errors="replace").split("\x00"):
                if item:
                    code, path = item[:2], item[3:]
                    found[path] = {"??": "add", " D": "delete", " M": "modify"}.get(code, code)
            expected = {c.path: c.kind for c in changes}
            if found != expected:
                raise ToolFailedError("applying the patches changed different files than they declare; not pushed")
            wanted = [c.path for c in changes if c.kind != "delete"]
            read = await sandboxes.read_files(key, wanted, max_bytes=MAX_PUSH_BYTES)
            return {p: data for p, (data, _) in read.items()}
        except SandboxError as exc:
            raise ToolFailedError(f"push sandbox failed: {exc}") from None
        finally:
            await sandboxes.destroy(key)

    async def _verify_base(
        self,
        sandboxes: DockerSandboxManager,
        key: SandboxKey,
        changes: list[FileChange],
        repo: GitHubRepo,
        base_sha: str,
    ) -> None:
        """The snapshot's version of every touched file must be GitHub's base blob (or absent for adds)."""
        base_tree = await self._github(repo.commit_tree(base_sha))
        cache: dict[str, Any] = {}
        existing = [c.path for c in changes if c.kind != "add"]
        hashes: dict[str, str] = {}
        if existing:
            r = await sandboxes.run(key, ["git", "hash-object", "--no-filters", "--", *existing])
            lines = r.stdout.decode().split()
            if r.exit_code != 0 or len(lines) != len(existing):
                raise ToolInputError("patch modifies or deletes a file that is not in the base commit")
            hashes = dict(zip(existing, lines, strict=True))
        for c in changes:
            entry = await self._base_entry(repo, base_tree, c.path, cache)
            if c.kind == "add":
                if entry is not None:
                    raise ToolInputError(f"{c.path} already exists in the base commit")
                continue
            if entry is None or entry["type"] != "blob" or entry["mode"] not in ("100644", "100755"):
                raise ToolInputError(f"{c.path} is not a regular file in the base commit")
            if entry["sha"] != hashes[c.path]:
                raise ToolFailedError(
                    f"{c.path}: the snapshot differs from the repository (export-subst or similar); not pushed"
                )

    # ------------------------------------------------------------------ github.create_pull_request (Level 3)
    async def create_pull_request(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        head = check_branch(args.get("head"), ctx.workflow_id)
        wr = await self.workflow_repo(ctx.org_id, ctx.workflow_id)
        title = str(args.get("title") or wr.title)[:200]
        tasks = args.get("task_keys") or []
        body = str(args.get("body") or "")[:20_000]
        if not body:
            body = f"Opened by AI-EOS for workflow `{ctx.workflow_id}`.\n\nTasks: " + (
                ", ".join(str(t) for t in tasks)[:2000] or "-"
            )
        pr = await self._github(
            self._app.repo(wr.repo).create_pull_request(head=head, base=wr.default_branch, title=title, body=body)
        )
        return {**pr, "head": head, "base": wr.default_branch}

    # ------------------------------------------------------------------ merge and comment (Level 3)
    async def _last_pushed_commit(self, org_id: UUID, workflow_id: UUID) -> str | None:
        async with self._db.tenant(org_id) as conn:
            value = (
                await conn.execute(
                    text(
                        """SELECT result->>'commit_sha' FROM tool_executions
                           WHERE workflow_id = :wf AND tool_name = 'github.push_branch' AND status = 'succeeded'
                           ORDER BY finished_at DESC NULLS LAST LIMIT 1"""
                    ),
                    {"wf": workflow_id},
                )
            ).scalar_one_or_none()
        return str(value) if value else None

    async def _workflow_pr(
        self, ctx: ToolContext, args: dict[str, Any]
    ) -> tuple[WorkflowRepo, GitHubRepo, dict[str, Any]]:
        number = args.get("pull_request")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ToolInputError("pull_request (number) is required")
        wr = await self.workflow_repo(ctx.org_id, ctx.workflow_id)
        repo = self._app.repo(wr.repo)
        pr = await self._github(repo.pull_request(number))
        try:
            check_branch(pr["head_ref"], ctx.workflow_id)
        except ToolInputError:
            raise ToolInputError(f"pull request #{number} was not opened for this workflow") from None
        if pr["head_repo"] and pr["head_repo"].lower() != wr.repo.full_name.lower():
            raise ToolInputError(f"pull request #{number} comes from another repository")
        return wr, repo, pr

    async def merge_pull_request(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        """Merge only the exact commit the platform pushed after review, into the default branch.

        `head_sha` is part of the approved request, so an approval covers one commit: if anyone pushes
        to the branch afterwards, the merge is refused here and by GitHub (`sha` precondition)."""
        expected = args.get("head_sha")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise ToolInputError("head_sha (40-character commit id) is required")
        wr, repo, pr = await self._workflow_pr(ctx, args)
        if pr["base_ref"] != wr.default_branch:
            raise ToolInputError(f"pull request targets {pr['base_ref']}, not {wr.default_branch}")
        if pr["merged"]:
            if pr["head_sha"] == expected:  # re-run after a crash: already done
                return {"merged": True, "number": pr["number"], "merge_commit_sha": pr["merge_commit_sha"]}
            raise ToolFailedError(f"pull request #{pr['number']} was merged with a different head")
        if pr["state"] != "open":
            raise ToolFailedError(f"pull request #{pr['number']} is {pr['state']}")
        pushed = await self._last_pushed_commit(ctx.org_id, ctx.workflow_id)
        if pr["head_sha"] != expected or expected != pushed:
            raise ToolFailedError(
                "the pull request head is not the commit the platform pushed after review; not merged"
            )
        merge_sha = await self._github(
            repo.merge_pull_request(pr["number"], head_sha=expected, title=f"{wr.title[:72]} (#{pr['number']})")
        )
        try:
            await repo.delete_branch(pr["head_ref"])
        except GitHubError as exc:  # the merge is what matters; a leftover aieos/* branch is harmless
            log.warning("github.branch_not_deleted", branch=pr["head_ref"], error=str(exc))
        log.info("github.merged", workflow_id=str(ctx.workflow_id), number=pr["number"], merge_sha=merge_sha)
        return {"merged": True, "number": pr["number"], "merge_commit_sha": merge_sha}

    async def comment(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        body = args.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > 20_000:
            raise ToolInputError("body (1 to 20000 characters) is required")
        _, repo, pr = await self._workflow_pr(ctx, args)
        result: dict[str, Any] = await self._github(repo.comment(pr["number"], body))
        return result

    def handlers(self) -> dict[str, Handler]:
        handlers: dict[str, Handler] = {
            "repo.list_tree": self.list_tree,
            "repo.read_file": self.read_file,
            "repo.search": self.search,
            "github.create_pull_request": self.create_pull_request,
            "github.merge_pull_request": self.merge_pull_request,
            "github.comment": self.comment,
        }
        if self._sandboxes is not None:
            handlers["github.push_branch"] = self.push_branch
        return handlers
