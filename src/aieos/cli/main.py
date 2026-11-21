"""`aieos`: user CLI for the Platform API.

    export AIEOS_API_URL=http://localhost:8000 AIEOS_TOKEN=... AIEOS_ORG=<org id>
    aieos approvals list
    aieos approvals approve <approval-id> [--comment "..."]
    aieos workflows show <workflow-id>

Uses only the standard library. The token is read from the environment, never from a flag,
so it does not end up in shell history.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

Send = Callable[[str, str, dict[str, Any] | None], tuple[int, Any]]


class ApiError(RuntimeError):
    def __init__(self, status: int, body: Any) -> None:
        message = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def http_sender(base_url: str, token: str, timeout: float = 30.0) -> Send:
    if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise SystemExit("AIEOS_API_URL must use https (plain http only for localhost)")

    def send(method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 - scheme validated above
            base_url.rstrip("/") + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else None

    return send


class Client:
    def __init__(self, send: Send, org_id: str) -> None:
        self._send = send
        self._org = org_id

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        status, payload = self._send(method, f"/v1/orgs/{self._org}{path}", body)
        if status >= 400:
            raise ApiError(status, payload)
        return payload

    def approvals(self, status: str = "pending") -> Any:
        return self._call("GET", f"/approvals?status={status}")

    def approval(self, approval_id: str) -> Any:
        return self._call("GET", f"/approvals/{approval_id}")

    def decide(self, approval_id: str, decision: str, comment: str | None) -> Any:
        current = self.approval(approval_id)  # carries the version for optimistic concurrency
        return self._call(
            "POST",
            f"/approvals/{approval_id}/decision",
            {"decision": decision, "version": current["version"], "comment": comment},
        )

    def workflow(self, workflow_id: str) -> Any:
        return self._call("GET", f"/workflows/{workflow_id}")

    def cancel(self, workflow_id: str, reason: str) -> Any:
        return self._call("POST", f"/workflows/{workflow_id}/cancel", {"reason": reason})

    def answer(self, workflow_id: str, answers: dict[str, str]) -> Any:
        return self._call("POST", f"/workflows/{workflow_id}/clarifications", {"answers": answers})


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def run(argv: list[str], client: Client) -> int:
    parser = argparse.ArgumentParser(prog="aieos")
    groups = parser.add_subparsers(dest="group", required=True)

    ap = groups.add_parser("approvals").add_subparsers(dest="cmd", required=True)
    p = ap.add_parser("list")
    p.add_argument("--status", default="pending")
    ap.add_parser("show").add_argument("approval_id")
    for verb in ("approve", "reject"):
        p = ap.add_parser(verb)
        p.add_argument("approval_id")
        p.add_argument("--comment")

    wf = groups.add_parser("workflows").add_subparsers(dest="cmd", required=True)
    wf.add_parser("show").add_argument("workflow_id")
    p = wf.add_parser("cancel")
    p.add_argument("workflow_id")
    p.add_argument("--reason", default="cancelled from CLI")
    p = wf.add_parser("answer", help="answer clarification questions: --answer Q-1='text' ...")
    p.add_argument("workflow_id")
    p.add_argument("--answer", action="append", required=True, metavar="ID=TEXT")

    args = parser.parse_args(argv)
    try:
        if args.group == "approvals":
            if args.cmd == "list":
                rows = client.approvals(args.status)
                for a in rows:
                    print(
                        f"{a['id']}  L{a['permission_level']}  {a['tool_name'] or a['kind']:<28} "
                        f"expires {a['expires_at']}  {a['summary'][:80]}"
                    )
                if not rows:
                    print("no approvals")
            elif args.cmd == "show":
                _print(client.approval(args.approval_id))
            else:
                result = client.decide(args.approval_id, "approve" if args.cmd == "approve" else "reject", args.comment)
                print(
                    f"{result['status']}: {result['id']}"
                    + ("" if result.get("signal_delivered") else " (workflow will pick it up on its next poll)")
                )
        else:
            if args.cmd == "show":
                _print(client.workflow(args.workflow_id))
            elif args.cmd == "cancel":
                _print(client.cancel(args.workflow_id, args.reason))
            else:
                pairs = [a.split("=", 1) for a in args.answer]
                if any(len(p) != 2 for p in pairs):
                    parser.error("--answer must look like ID=TEXT")
                _print(client.answer(args.workflow_id, dict(pairs)))
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    url, token, org = os.environ.get("AIEOS_API_URL"), os.environ.get("AIEOS_TOKEN"), os.environ.get("AIEOS_ORG")
    if not (url and token and org):
        print("set AIEOS_API_URL, AIEOS_TOKEN and AIEOS_ORG", file=sys.stderr)
        return 2
    return run(sys.argv[1:] if argv is None else argv, Client(http_sender(url, token), org))


if __name__ == "__main__":
    sys.exit(main())
