"""`aieos-admin`: operator commands.

Organization commands connect as the table owner (OWNER_DATABASE_URL), which is the
only provisioning path for tenants. They are never exposed through the API.
Dev commands generate a local signing key and tokens for local/test environments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import jwt
import psycopg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from aieos.agents.registry import ContractError, load_all
from aieos.core.ids import uuid7
from aieos.gateway.catalog import load_catalog

ROLES = ("owner", "admin", "approver", "member", "viewer")
DEV_KID = "aieos-dev-1"


def owner_conninfo(url: str) -> str:
    """Accept SQLAlchemy-style URLs (postgresql+psycopg://) as well as libpq ones."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _upsert_user(conn: psycopg.Connection[Any], sub: str, email: str) -> UUID:
    row = conn.execute(
        """INSERT INTO users (id, idp_subject, email) VALUES (%s, %s, %s)
           ON CONFLICT (idp_subject) DO UPDATE SET email = EXCLUDED.email, updated_at = now()
           RETURNING id""",
        (uuid7(), sub, email),
    ).fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _audit(
    conn: psycopg.Connection[Any], org_id: UUID, action: str, target_type: str, target_id: str, payload: dict[str, Any]
) -> None:
    conn.execute(
        """INSERT INTO audit_events (org_id, actor_type, actor_id, action, target_type, target_id, payload)
           VALUES (%s, 'system', 'aieos-admin', %s, %s, %s, %s::jsonb)""",
        (org_id, action, target_type, target_id, json.dumps(payload)),
    )


def create_org(conninfo: str, *, slug: str, name: str, owner_sub: str, owner_email: str) -> UUID:
    org_id = uuid7()
    with psycopg.connect(owner_conninfo(conninfo)) as conn:  # one transaction; commits on success
        conn.execute("INSERT INTO organizations (id, slug, name) VALUES (%s, %s, %s)", (org_id, slug, name))
        user_id = _upsert_user(conn, owner_sub, owner_email)
        conn.execute(
            "INSERT INTO memberships (id, org_id, user_id, role) VALUES (%s, %s, %s, 'owner')",
            (uuid7(), org_id, user_id),
        )
        _audit(conn, org_id, "organization.created", "organization", str(org_id), {"slug": slug})
        _audit(conn, org_id, "membership.granted", "user", str(user_id), {"role": "owner"})
    return org_id


def add_member(conninfo: str, *, org_slug: str, sub: str, email: str, role: str) -> UUID:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    with psycopg.connect(owner_conninfo(conninfo)) as conn:
        row = conn.execute("SELECT id FROM organizations WHERE slug = %s", (org_slug,)).fetchone()
        if row is None:
            raise LookupError(f"organization {org_slug!r} not found")
        org_id = UUID(str(row[0]))
        user_id = _upsert_user(conn, sub, email)
        conn.execute(
            """INSERT INTO memberships (id, org_id, user_id, role) VALUES (%s, %s, %s, %s)
               ON CONFLICT (org_id, user_id) DO UPDATE SET role = EXCLUDED.role""",
            (uuid7(), org_id, user_id, role),
        )
        _audit(conn, org_id, "membership.granted", "user", str(user_id), {"role": role})
    return user_id


def publish_agents(conninfo: str, engineering_dir: Path) -> list[str]:
    """Publish platform agent contracts (org_id NULL) as immutable agent_versions rows.

    Re-publishing an identical version is a no-op; publishing different content under an
    existing version is refused, so a version number always means one exact contract + prompt.
    """
    catalog = load_catalog(engineering_dir / "tool-catalog.yaml")
    loaded = load_all(engineering_dir / "agent-definitions", prompts_dir=engineering_dir / "prompts", catalog=catalog)
    report: list[str] = []
    with psycopg.connect(owner_conninfo(conninfo)) as conn:
        for key, item in sorted(loaded.items()):
            c = item.contract
            row = conn.execute(
                """INSERT INTO agents (id, org_id, key, name) VALUES (%s, NULL, %s, %s)
                   ON CONFLICT (org_id, key) DO UPDATE SET name = EXCLUDED.name
                   RETURNING id""",
                (uuid7(), key, c.name),
            ).fetchone()
            assert row is not None
            agent_id = row[0]
            existing = conn.execute(
                "SELECT contract_sha256, prompt_sha256 FROM agent_versions WHERE agent_id = %s AND version = %s",
                (agent_id, c.version),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (item.contract_sha256, item.prompt_sha256):
                    raise ContractError(
                        f"{key}@{c.version} is already published with different content; bump the version"
                    )
                report.append(f"{key}@{c.version} unchanged")
                continue
            conn.execute(
                """INSERT INTO agent_versions (id, org_id, agent_id, version, contract, contract_sha256, prompt_sha256)
                   VALUES (%s, NULL, %s, %s, %s::jsonb, %s, %s)""",
                (
                    uuid7(),
                    agent_id,
                    c.version,
                    json.dumps(c.model_dump(mode="json")),
                    item.contract_sha256,
                    item.prompt_sha256,
                ),
            )
            report.append(f"{key}@{c.version} published")
    return report


def dev_keys(out_dir: Path) -> Path:
    """Write a local RSA signing key and matching JWKS. Local/test use only."""
    out_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    private_path = out_dir / "private.pem"
    private_path.write_bytes(pem)
    os.chmod(private_path, 0o600)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": DEV_KID, "alg": "RS256", "use": "sig"})
    jwks_path = out_dir / "jwks.json"
    jwks_path.write_text(json.dumps({"keys": [jwk]}, indent=2))
    return jwks_path


def dev_token(
    key_dir: Path,
    *,
    issuer: str,
    audience: str,
    sub: str,
    email: str | None = None,
    ttl_seconds: int = 3600,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {"iss": issuer, "aud": audience, "sub": sub, "iat": now, "exp": now + ttl_seconds}
    if email:
        claims["email"] = email
    claims.update(extra_claims or {})
    private_key = (key_dir / "private.pem").read_bytes()
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": DEV_KID})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aieos-admin")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-org", help="create an organization and its first owner")
    p.add_argument("--slug", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--owner-sub", required=True, help="OIDC subject of the owner")
    p.add_argument("--owner-email", required=True)

    p = sub.add_parser("add-member", help="grant (or change) a user's role in an organization")
    p.add_argument("--org", required=True, help="organization slug")
    p.add_argument("--sub", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--role", required=True, choices=ROLES)

    p = sub.add_parser("publish-agents", help="publish agent contracts from engineering/ as immutable versions")
    p.add_argument("--engineering", type=Path, default=Path("engineering"))

    p = sub.add_parser("dev-keys", help="LOCAL ONLY: write a signing key and JWKS")
    p.add_argument("--out", type=Path, default=Path(".dev-keys"))

    p = sub.add_parser("dev-token", help="LOCAL ONLY: mint a bearer token signed with the dev key")
    p.add_argument("--keys", type=Path, default=Path(".dev-keys"))
    p.add_argument("--sub", required=True)
    p.add_argument("--email")
    p.add_argument("--ttl", type=int, default=3600)

    args = parser.parse_args(argv)
    if args.command in ("create-org", "add-member", "publish-agents"):
        url = os.environ.get("OWNER_DATABASE_URL")
        if not url:
            parser.error("OWNER_DATABASE_URL is not set")
        if args.command == "publish-agents":
            print("\n".join(publish_agents(url, args.engineering)))
        elif args.command == "create-org":
            print(
                create_org(url, slug=args.slug, name=args.name, owner_sub=args.owner_sub, owner_email=args.owner_email)
            )
        else:
            print(add_member(url, org_slug=args.org, sub=args.sub, email=args.email, role=args.role))
    elif args.command == "dev-keys":
        print(dev_keys(args.out))
    elif args.command == "dev-token":
        issuer, audience = os.environ.get("OIDC_ISSUER"), os.environ.get("OIDC_AUDIENCE")
        if not issuer or not audience:
            parser.error("OIDC_ISSUER and OIDC_AUDIENCE must be set")
        print(
            dev_token(args.keys, issuer=issuer, audience=audience, sub=args.sub, email=args.email, ttl_seconds=args.ttl)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
