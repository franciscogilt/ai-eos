"""In-memory GitHub REST API for tests (served through httpx.MockTransport).

Implements just what `aieos.integrations.github` uses: installation tokens, refs, commits,
trees (one level per request, like GitHub), blobs with real git blob ids, tarballs served by a
cross-host redirect, and pull requests. Trees and commits are flat path maps internally.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass, field
from typing import Any

import httpx

Files = dict[str, tuple[str, bytes]]  # path -> (mode, content)


def blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(content) + content, usedforsecurity=False).hexdigest()


def tarball(files: Files, top: str = "owner-repo-abc1234") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(top)
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        tar.addfile(info)
        for path, (mode, content) in sorted(files.items()):
            info = tarfile.TarInfo(f"{top}/{path}")
            info.size = len(content)
            info.mode = 0o755 if mode == "100755" else 0o644
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


@dataclass
class FakeGitHub:
    owner: str = "acme"
    repo: str = "app"
    blobs: dict[str, bytes] = field(default_factory=dict)
    commits: dict[str, dict[str, Any]] = field(default_factory=dict)  # sha -> {tree, parents, message}
    trees: dict[str, tuple[Files, str]] = field(default_factory=dict)  # sha -> (flat files of the root, dir prefix)
    refs: dict[str, str] = field(default_factory=dict)
    pulls: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    token_requests: list[dict[str, Any]] = field(default_factory=list)
    tarball_override: bytes | None = None

    # ------------------------------------------------------------------ setup helpers
    def _tree_id(self, files: Files, prefix: str = "") -> str:
        digest = hashlib.sha1(
            json.dumps([prefix, sorted((p, m, blob_sha(c)) for p, (m, c) in files.items())]).encode(),
            usedforsecurity=False,
        ).hexdigest()
        self.trees[digest] = (files, prefix)
        return digest

    def commit(self, files: Files, *, parent: str | None = None, message: str = "base") -> str:
        for _, content in files.values():
            self.blobs[blob_sha(content)] = content
        tree = self._tree_id(files)
        sha = hashlib.sha1(f"{tree}{parent}{message}".encode(), usedforsecurity=False).hexdigest()
        self.commits[sha] = {"tree": tree, "parents": [parent] if parent else [], "message": message, "files": files}
        return sha

    def files_at(self, branch: str) -> Files:
        return dict(self.commits[self.refs[branch]]["files"])

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ API
    def _entries(self, tree_sha: str) -> list[dict[str, Any]]:
        files, prefix = self.trees[tree_sha]
        entries: dict[str, dict[str, Any]] = {}
        for path, (mode, content) in files.items():
            if not path.startswith(prefix):
                continue
            head, sep, _ = path[len(prefix) :].partition("/")
            if sep:
                entries[head] = {
                    "path": head,
                    "mode": "040000",
                    "type": "tree",
                    "sha": self._tree_id(files, f"{prefix}{head}/"),
                }
            else:
                entries[head] = {"path": head, "mode": mode, "type": "blob", "sha": blob_sha(content)}
        return list(entries.values())

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if request.url.host == "codeload.test":
            sha = path.rsplit("/", 1)[-1]
            data = self.tarball_override or tarball(self.commits[sha]["files"])
            return httpx.Response(200, content=data)
        if m := re.fullmatch(r"/app/installations/(\d+)/access_tokens", path):
            self.token_requests.append(body)
            return httpx.Response(
                201, json={"token": f"ghs_test_{len(self.token_requests)}", "expires_at": "2099-01-01T00:00:00Z"}
            )
        base = f"/repos/{self.owner}/{self.repo}"
        if not path.startswith(base):
            return httpx.Response(404, json={"message": "Not Found"})
        sub = path[len(base) :]
        method = request.method
        if method == "GET" and (m := re.fullmatch(r"/git/ref/heads/(.+)", sub)):
            sha = self.refs.get(m.group(1))
            return (
                httpx.Response(200, json={"object": {"sha": sha}})
                if sha
                else httpx.Response(404, json={"message": "Not Found"})
            )
        if method == "GET" and (m := re.fullmatch(r"/git/commits/(\w+)", sub)):
            c = self.commits[m.group(1)]
            return httpx.Response(200, json={"sha": m.group(1), "tree": {"sha": c["tree"]}})
        if method == "GET" and (m := re.fullmatch(r"/git/trees/(\w+)", sub)):
            return httpx.Response(200, json={"sha": m.group(1), "tree": self._entries(m.group(1)), "truncated": False})
        if method == "GET" and (m := re.fullmatch(r"/tarball/(\w+)", sub)):
            return httpx.Response(
                302, headers={"location": f"https://codeload.test/{self.owner}/{self.repo}/legacy.tar.gz/{m.group(1)}"}
            )
        if method == "POST" and sub == "/git/blobs":
            content = base64.b64decode(body["content"])
            sha = blob_sha(content)
            self.blobs[sha] = content
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and sub == "/git/trees":
            files = dict(self.trees[body["base_tree"]][0])
            for e in body["tree"]:
                if e["sha"] is None:
                    files.pop(e["path"], None)
                else:
                    files[e["path"]] = (e["mode"], self.blobs[e["sha"]])
            return httpx.Response(201, json={"sha": self._tree_id(files)})
        if method == "POST" and sub == "/git/commits":
            files = self.trees[body["tree"]][0]
            sha = self.commit(files, parent=body["parents"][0], message=body["message"])
            return httpx.Response(201, json={"sha": sha})
        if method == "POST" and sub == "/git/refs":
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[name] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"]})
        if method == "PATCH" and (m := re.fullmatch(r"/git/refs/heads/(.+)", sub)):
            self.refs[m.group(1)] = body["sha"]
            return httpx.Response(200, json={})
        if method == "DELETE" and (m := re.fullmatch(r"/git/refs/heads/(.+)", sub)):
            self.refs.pop(m.group(1), None)
            return httpx.Response(204)
        if method == "POST" and sub == "/pulls":
            if any(p["head"]["ref"] == body["head"] for p in self.pulls):
                return httpx.Response(422, json={"message": "A pull request already exists"})
            pr = {
                "number": len(self.pulls) + 1,
                "html_url": f"https://github.test/{self.owner}/{self.repo}/pull/{len(self.pulls) + 1}",
                "head": {"ref": body["head"], "sha": self.refs.get(body["head"])},
                "base": {"ref": body["base"]},
                "title": body["title"],
            }
            self.pulls.append(pr)
            return httpx.Response(201, json=pr)
        if method == "GET" and sub == "/pulls":
            head = request.url.params["head"].split(":", 1)[1]
            return httpx.Response(200, json=[p for p in self.pulls if p["head"]["ref"] == head])
        return httpx.Response(404, json={"message": f"fake: no route {method} {sub}"})
