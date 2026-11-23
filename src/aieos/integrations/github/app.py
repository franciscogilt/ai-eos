"""GitHub App client (REST over httpx). Only the tool-gateway worker holds the App's private key.

* Every installation token is scoped down to ONE repository and the minimum permissions of the
  call (contents:read for snapshots, contents:write for pushes, pull_requests:write for PRs),
  even though the installation may grant more. Tokens are cached until five minutes before expiry.
* Tokens and the private key never leave this module: they are not logged, returned or passed
  to a sandbox. Error messages carry GitHub's message, never request headers.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from aieos.core.logging import get_logger

log = get_logger(__name__)

API_VERSION = "2022-11-28"
DEFAULT_API_URL = "https://api.github.com"
_TOKEN_REFRESH_MARGIN_S = 300


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, transient: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.transient = transient


def _check(response: httpx.Response, what: str) -> None:
    status = response.status_code
    if status < 400:
        return
    rate_limited = status in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0"
    transient = status >= 500 or status == 429 or rate_limited
    try:
        message = str(response.json().get("message", ""))[:300]
    except ValueError:
        message = response.text[:300]
    raise GitHubError(f"GitHub {what}: HTTP {status}: {message}", status=status, transient=transient)


@dataclass(frozen=True, slots=True)
class RepoRef:
    owner: str
    name: str
    installation_id: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


class GitHubApp:
    def __init__(
        self,
        app_id: str,
        private_key_pem: str,
        *,
        api_url: str = DEFAULT_API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not app_id or "BEGIN" not in private_key_pem:
            raise ValueError("GitHub App id and PEM private key are required")
        self._app_id = app_id
        self._key = private_key_pem
        self._clock = clock
        self._tokens: dict[tuple[int, str, tuple[tuple[str, str], ...]], tuple[str, float]] = {}
        self.http = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            headers={
                "accept": "application/vnd.github+json",
                "x-github-api-version": API_VERSION,
                "user-agent": "aieos-gateway",
            },
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            transport=transport,
        )

    def app_jwt(self) -> str:
        now = int(self._clock())
        # iat in the past absorbs clock skew; GitHub accepts at most 10 minutes of validity.
        claims = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode(claims, self._key, algorithm="RS256")

    async def installation_token(self, repo: RepoRef, permissions: dict[str, str]) -> str:
        key = (repo.installation_id, repo.name, tuple(sorted(permissions.items())))
        cached = self._tokens.get(key)
        if cached and cached[1] - _TOKEN_REFRESH_MARGIN_S > self._clock():
            return cached[0]
        try:
            response = await self.http.post(
                f"/app/installations/{repo.installation_id}/access_tokens",
                headers={"authorization": f"Bearer {self.app_jwt()}"},
                json={"repositories": [repo.name], "permissions": permissions},
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub token request failed: {type(exc).__name__}", transient=True) from None
        _check(response, "installation token")
        data = response.json()
        expires = datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00")).timestamp()
        self._tokens[key] = (str(data["token"]), expires)
        return str(data["token"])

    def repo(self, ref: RepoRef) -> GitHubRepo:
        return GitHubRepo(self, ref)

    async def aclose(self) -> None:
        await self.http.aclose()


READ = {"contents": "read"}
WRITE = {"contents": "write"}
PULLS = {"pull_requests": "write"}
MERGE = {"contents": "write", "pull_requests": "write"}


class GitHubRepo:
    """Operations on one repository. Paths are relative to /repos/{owner}/{name}."""

    def __init__(self, app: GitHubApp, ref: RepoRef) -> None:
        self._app = app
        self.ref = ref
        self._base = f"/repos/{quote(ref.owner, safe='')}/{quote(ref.name, safe='')}"

    async def _call(
        self,
        method: str,
        path: str,
        permissions: dict[str, str],
        *,
        what: str,
        json: Any = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        token = await self._app.installation_token(self.ref, permissions)
        try:
            response = await self._app.http.request(
                method,
                self._base + path,
                headers={"authorization": f"Bearer {token}"},
                json=json,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub {what} failed: {type(exc).__name__}", transient=True) from None
        _check(response, what)
        return response

    # ------------------------------------------------------------------ reads
    async def branch_head(self, branch: str) -> str:
        r = await self._call("GET", f"/git/ref/heads/{quote(branch, safe='/')}", READ, what="read branch")
        return str(r.json()["object"]["sha"])

    async def commit_tree(self, commit_sha: str) -> str:
        r = await self._call("GET", f"/git/commits/{commit_sha}", READ, what="read commit")
        return str(r.json()["tree"]["sha"])

    async def tree(self, tree_sha: str) -> list[dict[str, Any]]:
        r = await self._call("GET", f"/git/trees/{tree_sha}", READ, what="read tree")
        return list(r.json().get("tree", []))

    async def tarball(self, commit_sha: str, *, max_bytes: int) -> bytes:
        """The repository at `commit_sha` as a gzipped tarball (GitHub redirects to codeload; httpx
        drops the Authorization header on that cross-host redirect, the redirect URL carries its own grant)."""
        token = await self._app.installation_token(self.ref, READ)
        chunks: list[bytes] = []
        size = 0
        try:
            async with self._app.http.stream(
                "GET",
                f"{self._base}/tarball/{commit_sha}",
                headers={"authorization": f"Bearer {token}"},
                follow_redirects=True,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    _check(response, "download snapshot")
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise GitHubError(f"repository snapshot is larger than {max_bytes} bytes")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub download snapshot failed: {type(exc).__name__}", transient=True) from None
        return b"".join(chunks)

    async def file_text(self, branch: str, path: str) -> str | None:
        """For tests and verification: a file's content on a branch, or None if missing."""
        try:
            r = await self._call(
                "GET", f"/contents/{quote(path)}", READ, what="read file", params={"ref": f"refs/heads/{branch}"}
            )
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        return base64.b64decode(r.json()["content"]).decode("utf-8")

    # ------------------------------------------------------------------ writes
    async def create_blob(self, content: bytes) -> str:
        r = await self._call(
            "POST",
            "/git/blobs",
            WRITE,
            what="create blob",
            json={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        return str(r.json()["sha"])

    async def create_tree(self, base_tree: str, entries: list[dict[str, Any]]) -> str:
        r = await self._call(
            "POST", "/git/trees", WRITE, what="create tree", json={"base_tree": base_tree, "tree": entries}
        )
        return str(r.json()["sha"])

    async def create_commit(self, message: str, tree: str, parent: str) -> str:
        r = await self._call(
            "POST",
            "/git/commits",
            WRITE,
            what="create commit",
            json={"message": message, "tree": tree, "parents": [parent]},
        )
        return str(r.json()["sha"])

    async def set_branch(self, branch: str, commit_sha: str) -> None:
        """Create the branch, or move it to `commit_sha` (callers allow this only for aieos/* branches)."""
        try:
            await self._call(
                "POST",
                "/git/refs",
                WRITE,
                what="create branch",
                json={"ref": f"refs/heads/{branch}", "sha": commit_sha},
            )
        except GitHubError as exc:
            if exc.status != 422:
                raise
            await self._call(
                "PATCH",
                f"/git/refs/heads/{quote(branch, safe='/')}",
                WRITE,
                what="update branch",
                json={"sha": commit_sha, "force": True},
            )

    async def delete_branch(self, branch: str) -> None:
        await self._call("DELETE", f"/git/refs/heads/{quote(branch, safe='/')}", WRITE, what="delete branch")

    async def create_pull_request(self, *, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        try:
            r = await self._call(
                "POST",
                "/pulls",
                PULLS,
                what="create pull request",
                json={"head": head, "base": base, "title": title, "body": body, "maintainer_can_modify": False},
            )
            data = r.json()
        except GitHubError as exc:
            if exc.status != 422:
                raise
            # Re-running the step: return the open PR for this branch instead of failing.
            r = await self._call(
                "GET",
                "/pulls",
                PULLS,
                what="find pull request",
                params={"head": f"{self.ref.owner}:{head}", "base": base, "state": "open"},
            )
            found = r.json()
            if not found:
                raise
            data = found[0]
        return {"number": int(data["number"]), "url": str(data["html_url"]), "head_sha": data["head"]["sha"]}

    async def pull_request(self, number: int) -> dict[str, Any]:
        r = await self._call("GET", f"/pulls/{int(number)}", PULLS, what="read pull request")
        data = r.json()
        return {
            "number": int(data["number"]),
            "state": str(data["state"]),
            "merged": bool(data.get("merged")),
            "head_ref": str(data["head"]["ref"]),
            "head_sha": str(data["head"]["sha"]),
            "head_repo": str(((data["head"].get("repo") or {}).get("full_name")) or ""),
            "base_ref": str(data["base"]["ref"]),
            "merge_commit_sha": data.get("merge_commit_sha"),
            "url": str(data.get("html_url", "")),
        }

    async def merge_pull_request(self, number: int, *, head_sha: str, title: str) -> str:
        """Squash-merge, only if the head is still `head_sha` (GitHub refuses otherwise). Branch
        protection and required checks are enforced by GitHub; the App cannot bypass them."""
        r = await self._call(
            "PUT",
            f"/pulls/{int(number)}/merge",
            MERGE,
            what="merge pull request",
            json={"sha": head_sha, "merge_method": "squash", "commit_title": title},
        )
        return str(r.json()["sha"])

    async def comment(self, number: int, body: str) -> dict[str, Any]:
        r = await self._call("POST", f"/issues/{int(number)}/comments", PULLS, what="comment", json={"body": body})
        data = r.json()
        return {"comment_id": int(data["id"]), "url": str(data.get("html_url", ""))}
