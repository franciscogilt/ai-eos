"""In-sandbox command executor for the Fargate backend (Python standard library only).

It runs as the sandbox task's main process, as root, and executes each requested command as the
unprivileged sandbox user (uid/gid 65534) in its own process group, without a shell, with a clean
environment and a hard timeout. Because commands run as another user and the root filesystem is
read-only, code in the sandbox cannot kill, replace or impersonate the executor, so the exit codes and
outputs the gateway receives (test results, patches) are the real ones.

Every request must carry the per-sandbox token (HMAC of the sandbox name, derived by the gateway; see
sandbox/fargate.py). The token is removed from this process's environment at startup and never given to
commands. The task's security group admits only the gateway, and its subnets have no route out.

Wire format (POST /exec, Authorization: Bearer <token>), both directions:
    4-byte big-endian header length | JSON header | payload
request header {"argv": [...], "timeout": s, "workdir": "<path relative to the workspace>"}; payload = stdin.
response header {"exit_code", "timed_out", "stdout_len", "stderr_len"}; payload = stdout + stderr.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import resource
import signal
import struct
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 320 * 1024 * 1024  # a 200 MB repository snapshot plus framing
MAX_TIMEOUT_SECONDS = 3600
CHILD_ENV = {
    "HOME": "/tmp",  # noqa: S108 - path inside the sandbox
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def frame(header: dict[str, Any], payload: bytes = b"") -> bytes:
    raw = json.dumps(header).encode()
    return struct.pack(">I", len(raw)) + raw + payload


def unframe(data: bytes) -> tuple[dict[str, Any], bytes]:
    if len(data) < 4:
        raise ValueError("short frame")
    (n,) = struct.unpack(">I", data[:4])
    if n > 1_000_000 or 4 + n > len(data):
        raise ValueError("bad header length")
    header = json.loads(data[4 : 4 + n])
    if not isinstance(header, dict):
        raise ValueError("header must be an object")
    return header, data[4 + n :]


class Executor:
    def __init__(self, token: str, workspace: Path, run_as: tuple[int, int] | None, nproc: int = 512) -> None:
        if len(token) < 32:
            raise ValueError("executor token must be at least 32 characters")
        self.token = token
        self.workspace = workspace.resolve()
        self.run_as = run_as
        self.nproc = nproc

    def authorized(self, header: str | None) -> bool:
        expected = f"Bearer {self.token}"
        return header is not None and hmac.compare_digest(header.encode(), expected.encode())

    def _demote(self) -> None:  # runs in the child, before exec
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if self.run_as is not None:
            uid, gid = self.run_as
            resource.setrlimit(resource.RLIMIT_NPROC, (self.nproc, self.nproc))
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

    def run(self, header: dict[str, Any], stdin: bytes) -> tuple[dict[str, Any], bytes]:
        argv = header.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv must be a non-empty list of strings")
        timeout = int(header.get("timeout") or 900)
        if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout out of range")
        relative = str(header.get("workdir") or "")
        if relative.startswith("/"):
            raise ValueError("workdir must be inside the workspace (relative path)")
        workdir = (self.workspace / relative).resolve()
        if workdir != self.workspace and self.workspace not in workdir.parents:
            raise ValueError("workdir must be inside the workspace")
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell; this is the sandbox's purpose
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=dict(CHILD_ENV),
            preexec_fn=self._demote,
            close_fds=True,
        )
        timed_out = False
        try:
            out, err = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
        code = proc.returncode if proc.returncode is not None else -1
        if code < 0:
            code = 128 - code  # killed by a signal: same convention as `timeout -s KILL` (137)
        return {"exit_code": code, "timed_out": timed_out, "stdout_len": len(out), "stderr_len": len(err)}, out + err


def make_handler(executor: Executor) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "aieos-executor"

        def log_message(self, fmt: str, *args: Any) -> None:  # no request logging (arguments may be sensitive)
            return

        def _send(self, status: int, body: bytes, content_type: str = "application/octet-stream") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send(200, b"ok", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if self.path != "/exec":
                self._send(404, b"not found", "text/plain")
                return
            if not executor.authorized(self.headers.get("Authorization")):
                self._send(401, b"unauthorized", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_REQUEST_BYTES:
                self._send(413, b"request too large", "text/plain")
                return
            try:
                header, stdin = unframe(self.rfile.read(length))
                result, payload = executor.run(header, stdin)
            except (ValueError, OSError) as exc:
                self._send(400, str(exc).encode()[:500], "text/plain")
                return
            self._send(200, frame(result, payload))

    return Handler


def main() -> None:
    token = os.environ.pop("AIEOS_EXECUTOR_TOKEN", "")
    workspace = Path(os.environ.get("AIEOS_WORKSPACE", "/workspace"))
    port = int(os.environ.get("AIEOS_EXECUTOR_PORT", "7070"))
    bind = os.environ.get("AIEOS_EXECUTOR_BIND", "0.0.0.0")  # noqa: S104 - the security group limits callers
    run_as: tuple[int, int] | None = None
    if os.geteuid() == 0:
        run_as = (65534, 65534)
        workspace.mkdir(parents=True, exist_ok=True)
        os.chown(workspace, *run_as)
        os.chmod(workspace, 0o700)
    executor = Executor(token, workspace, run_as)
    server = ThreadingHTTPServer((bind, port), make_handler(executor))
    print(f"aieos executor listening on {server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
