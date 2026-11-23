"""GitHub integration (B13) without network: patch validation, App auth, REST client, repo tools."""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from tests.github_fake import FakeGitHub, tarball

from aieos.gateway.catalog import ToolSpec
from aieos.gateway.github_handlers import GitHubTools, WorkflowRepo, check_branch, git_blob_sha
from aieos.gateway.handlers import ToolContext, ToolFailedError, ToolInputError, ToolTransientError
from aieos.integrations.github.app import GitHubApp, GitHubError, RepoRef
from aieos.integrations.github.patch import PatchRejectedError, validate_patch

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
REF = RepoRef(owner="acme", name="app", installation_id=42)


def app(fake: FakeGitHub | None = None, *, transport: Any = None, clock=time.time) -> GitHubApp:
    return GitHubApp(
        "12345",
        PEM,
        api_url="https://api.github.test",
        transport=transport or (fake or FakeGitHub()).transport(),
        clock=clock,
    )


# ------------------------------------------------------------------ patch validation
MODIFY = b"""diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a + b  # checked
"""
ADD = b"""diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1 @@
+def test_x(): pass
"""
DELETE = b"""diff --git a/old.txt b/old.txt
deleted file mode 100644
index 4444444..0000000
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
"""
RENAME = b"""diff --git a/a.txt b/docs/a.txt
similarity index 100%
rename from a.txt
rename to docs/a.txt
"""
CHMOD = b"""diff --git a/run.sh b/run.sh
old mode 100644
new mode 100755
"""


def test_valid_patch_kinds():
    changes = validate_patch(MODIFY + ADD + DELETE + RENAME + CHMOD)
    assert [(c.path, c.kind, c.mode) for c in changes] == [
        ("src/app.py", "modify", None),
        ("tests/test_new.py", "add", "100644"),
        ("old.txt", "delete", None),
        ("a.txt", "delete", None),
        ("docs/a.txt", "add", None),
        ("run.sh", "modify", "100755"),
    ]


def _header(path: str, extra: str = "") -> bytes:
    return f"diff --git a/{path} b/{path}\n{extra}--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n".encode()


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        (b"", "empty"),
        (MODIFY.replace(b"checked", b"\x00"), "binary"),
        (MODIFY.replace(b"checked", b"\xff\xfe"), "binary"),
        (b"diff --git a/logo.png b/logo.png\nindex 1..2 100644\nGIT binary patch\nliteral 10\n", "binary"),
        (
            b"diff --git a/logo.png b/logo.png\nindex 1..2 100644\nBinary files a/logo.png and b/logo.png differ\n",
            "binary",
        ),
        (_header(".gitattributes"), ".gitattributes"),
        (_header("sub/dir/.GitAttributes"), ".GitAttributes"),
        (_header(".gitmodules"), ".gitmodules"),
        (_header(".github/workflows/ci.yml"), "workflows"),
        (_header(".GitHub/Workflows/ci.yml"), "workflows"),
        (_header(".git/config"), ".git"),
        (_header("a/.git/hooks/pre-commit"), ".git"),
        (
            b"diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+/etc\n",
            "symlink",
        ),
        (b"diff --git a/vendor b/vendor\nnew file mode 160000\nindex 0..1\n", "submodule"),
        (_header("x.sh", "old mode 100644\nnew mode 120000\n"), "symlink"),
        (_header("-rf"), "unsupported path"),
        (b'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\n', "unsupported diff header"),
        (_header("a/../../etc/passwd"), "normalised"),
        (b"diff --git a/x b/y\nindex 1..2 100644\n--- a/x\n+++ b/y\n", "inconsistent"),
        (b"diff --git a/x b/y\nsimilarity index 90%\ncopy from x\ncopy to y\n", "copies"),
        (b"hello\n" + MODIFY, "before the first diff header"),
        (MODIFY + MODIFY, "same path twice"),
        (b"".join(_header(f"f{i}.txt") for i in range(201)), "more than 200"),
    ],
)
def test_patch_rejections(patch, reason):
    with pytest.raises(PatchRejectedError, match=reason):
        validate_patch(patch)


def test_branch_rules():
    wf = uuid.uuid4()
    assert check_branch(f"aieos/{wf}", wf) == f"aieos/{wf}"
    assert check_branch(f"aieos/{wf}/T-1", wf)
    for bad in (
        "main",
        f"aieos/{uuid.uuid4()}",
        f"aieos/{wf}x",
        f"aieos/{wf}/../main",
        f"aieos/{wf}/a.lock",
        f"aieos/{wf}/.hidden",
        f"aieos/{wf}//x",
        f"aieos/{wf}/a b",
        None,
    ):
        with pytest.raises(ToolInputError):
            check_branch(bad, wf)


# ------------------------------------------------------------------ App auth
def test_app_jwt_is_short_lived_and_signed():
    token = app().app_jwt()
    claims = jwt.decode(token, KEY.public_key(), algorithms=["RS256"])
    assert claims["iss"] == "12345" and claims["exp"] - claims["iat"] <= 600
    assert claims["iat"] <= time.time()


def test_installation_tokens_are_scoped_and_cached():
    fake = FakeGitHub()
    now = [1_000.0]
    a = app(fake, clock=lambda: now[0])
    read = asyncio.run(a.installation_token(REF, {"contents": "read"}))
    again = asyncio.run(a.installation_token(REF, {"contents": "read"}))
    write = asyncio.run(a.installation_token(REF, {"contents": "write"}))
    assert read == again != write
    assert fake.token_requests == [
        {"repositories": ["app"], "permissions": {"contents": "read"}},
        {"repositories": ["app"], "permissions": {"contents": "write"}},
    ]
    assert fake.requests[0].headers["authorization"].startswith("Bearer ey")  # the App JWT, never a token


def test_expiring_token_is_refreshed():
    expiry = "2026-01-01T00:10:00Z"
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(201, json={"token": f"t{len(calls)}", "expires_at": expiry})

    from datetime import datetime

    t_exp = datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
    now = [t_exp - 600]
    a = app(transport=httpx.MockTransport(handler), clock=lambda: now[0])
    assert asyncio.run(a.installation_token(REF, {"contents": "read"})) == "t1"
    now[0] = t_exp - 200  # inside the 5-minute margin
    assert asyncio.run(a.installation_token(REF, {"contents": "read"})) == "t2"


@pytest.mark.parametrize(
    ("status", "headers", "transient"),
    [
        (500, {}, True),
        (502, {}, True),
        (429, {}, True),
        (403, {"x-ratelimit-remaining": "0"}, True),
        (403, {}, False),
        (404, {}, False),
        (422, {}, False),
    ],
)
def test_error_classification(status, headers, transient):
    def handler(request: httpx.Request) -> httpx.Response:
        if "access_tokens" in request.url.path:
            return httpx.Response(201, json={"token": "ghs_secret_value", "expires_at": "2099-01-01T00:00:00Z"})
        return httpx.Response(status, headers=headers, json={"message": "nope"})

    repo = app(transport=httpx.MockTransport(handler)).repo(REF)
    with pytest.raises(GitHubError) as err:
        asyncio.run(repo.branch_head("main"))
    assert err.value.transient is transient and err.value.status == status
    assert "ghs_secret_value" not in str(err.value)


def test_network_error_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(GitHubError) as err:
        asyncio.run(app(transport=httpx.MockTransport(handler)).installation_token(REF, {"contents": "read"}))
    assert err.value.transient


# ------------------------------------------------------------------ REST operations
def seeded() -> tuple[FakeGitHub, str]:
    fake = FakeGitHub()
    base = fake.commit({"README.md": ("100644", b"# app\n"), "src/app.py": ("100644", b"print(1)\n")})
    fake.refs["main"] = base
    return fake, base


def test_tarball_follows_redirect_without_leaking_the_token():
    fake, base = seeded()
    data = asyncio.run(app(fake).repo(REF).tarball(base, max_bytes=10**6))
    assert data[:2] == b"\x1f\x8b"
    codeload = [r for r in fake.requests if r.url.host == "codeload.test"]
    assert len(codeload) == 1 and "authorization" not in codeload[0].headers
    with pytest.raises(GitHubError, match="larger than"):
        asyncio.run(app(fake).repo(REF).tarball(base, max_bytes=10))


def test_branch_create_then_force_move_and_pr_reuse():
    fake, base = seeded()
    repo = app(fake).repo(REF)
    asyncio.run(repo.set_branch("aieos/x", base))
    other = fake.commit({"a": ("100644", b"1")}, parent=base, message="m")
    asyncio.run(repo.set_branch("aieos/x", other))  # 422 on create -> PATCH with force
    assert fake.refs["aieos/x"] == other
    assert [r.method for r in fake.requests if "/git/refs" in r.url.path][-2:] == ["POST", "PATCH"]
    first = asyncio.run(repo.create_pull_request(head="aieos/x", base="main", title="t", body="b"))
    second = asyncio.run(repo.create_pull_request(head="aieos/x", base="main", title="t", body="b"))
    assert first["number"] == second["number"] == 1
    assert fake.token_requests[-1]["permissions"] == {"pull_requests": "write"}


# ------------------------------------------------------------------ repo.* tools
class StubTools(GitHubTools):
    def __init__(self, fake: FakeGitHub, base: str, *, store: Any = None, sandboxes: Any = None) -> None:
        super().__init__(db=None, app=app(fake), store=store, sandboxes=sandboxes)  # type: ignore[arg-type]
        self._wr = WorkflowRepo(REF, "main", base, "Add CSV export")

    async def workflow_repo(self, org_id, workflow_id):  # no database in unit tests
        return self._wr


def ctx(tool: str = "repo.read_file", workflow_id: uuid.UUID | None = None) -> ToolContext:
    spec = ToolSpec.model_validate(
        {"name": tool, "level": 1, "description": "d", "executes_in": "gateway", "idempotent": True}
    )
    return ToolContext(uuid.uuid4(), workflow_id or uuid.uuid4(), uuid.uuid4(), "requirements", spec)


def test_repo_tools_read_the_snapshot():
    fake = FakeGitHub()
    base = fake.commit(
        {
            "README.md": ("100644", b"# Reports\nExport CSV soon\n"),
            "src/reports/api.py": ("100644", b"def export_csv():\n    raise NotImplementedError\n"),
            "assets/logo.png": ("100644", b"\x89PNG\x00\x00"),
        }
    )
    tools = StubTools(fake, base)
    c = ctx()
    root = asyncio.run(tools.list_tree(c, {"path": None}))
    assert [e["name"] for e in root["entries"]] == ["assets", "src", "README.md"]
    assert asyncio.run(tools.read_file(c, {"path": "src/reports/api.py"}))["text"].startswith("def export_csv")
    assert asyncio.run(tools.read_file(c, {"path": "assets/logo.png"}))["binary"] is True
    hits = asyncio.run(tools.search(c, {"query": "csv", "path": None}))["matches"]
    assert {(h["path"], h["line"]) for h in hits} == {("README.md", 2), ("src/reports/api.py", 1)}
    with pytest.raises(ToolInputError):
        asyncio.run(tools.read_file(c, {"path": "../etc/passwd"}))
    with pytest.raises(ToolInputError):
        asyncio.run(tools.search(c, {"query": "x"}))
    tarball_downloads = [r for r in fake.requests if r.url.host == "codeload.test"]
    assert len(tarball_downloads) == 1  # cached per (repository, base commit)


def test_snapshot_size_limit_is_enforced(monkeypatch):
    import aieos.gateway.github_handlers as gh

    fake = FakeGitHub()
    base = fake.commit({"big.bin": ("100644", b"0" * 5000)})
    monkeypatch.setattr(gh, "MAX_UNPACKED_BYTES", 1000)
    with pytest.raises(ToolFailedError, match="larger than"):
        asyncio.run(StubTools(fake, base).read_file(ctx(), {"path": "big.bin"}))


def test_snapshot_transient_github_error_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        if "access_tokens" in request.url.path:
            return httpx.Response(201, json={"token": "t", "expires_at": "2099-01-01T00:00:00Z"})
        return httpx.Response(503)

    tools = StubTools(FakeGitHub(), "0" * 40)
    tools._app = app(transport=httpx.MockTransport(handler))
    with pytest.raises(ToolTransientError):
        asyncio.run(tools.read_file(ctx(), {"path": "x"}))


# ------------------------------------------------------------------ push: checks before any sandbox or write
class FakeStore:
    def __init__(self, kind: str, workflow_id: uuid.UUID, data: bytes) -> None:
        self.row = SimpleNamespace(kind=kind, workflow_id=workflow_id, sha256="f" * 64)
        self.data = data

    async def get(self, org_id, artifact_id):
        return self.row, self.data


@pytest.mark.parametrize(
    ("kind", "same_workflow", "patch", "branch_suffix", "match"),
    [
        ("patch", True, MODIFY, "/../main", "branch"),
        ("requirements_spec", True, MODIFY, "", "patch artifact of this workflow"),
        ("patch", False, MODIFY, "", "patch artifact of this workflow"),
        ("patch", True, _header(".github/workflows/x.yml"), "", "workflows"),
    ],
)
def test_push_rejects_before_touching_github(kind, same_workflow, patch, branch_suffix, match):
    fake, base = seeded()
    wf = uuid.uuid4()
    store = FakeStore(kind, wf if same_workflow else uuid.uuid4(), patch)
    tools = StubTools(fake, base, store=store, sandboxes=object())
    with pytest.raises(ToolInputError, match=match):
        asyncio.run(
            tools.push_branch(
                ctx("github.push_branch", wf),
                {"patch_artifact_id": str(uuid.uuid4()), "branch": f"aieos/{wf}{branch_suffix}"},
            )
        )
    assert not any(r.method in ("POST", "PATCH") and "/repos/" in r.url.path for r in fake.requests)


def test_push_handler_absent_without_sandbox_backend():
    fake, base = seeded()
    assert "github.push_branch" not in StubTools(fake, base).handlers()
    assert "github.push_branch" in StubTools(fake, base, sandboxes=object()).handlers()


def test_git_blob_sha_matches_git():
    assert git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_pull_request_uses_default_branch_and_workflow_branch():
    fake, base = seeded()
    wf = uuid.uuid4()
    fake.refs[f"aieos/{wf}"] = base
    tools = StubTools(fake, base)
    pr = asyncio.run(
        tools.create_pull_request(ctx("github.create_pull_request", wf), {"head": f"aieos/{wf}", "task_keys": ["T-1"]})
    )
    assert pr["base"] == "main" and pr["number"] == 1 and fake.pulls[0]["title"] == "Add CSV export"
    with pytest.raises(ToolInputError):
        asyncio.run(tools.create_pull_request(ctx("github.create_pull_request", wf), {"head": "main"}))


def test_fake_tarball_helper_roundtrip():
    assert tarball({"a.txt": ("100644", b"x")})[:2] == b"\x1f\x8b"
