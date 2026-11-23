"""Database URLs from environment variables.

Locally a full URL is set (DATABASE_URL, OWNER_DATABASE_URL). On AWS, ECS injects the parts instead,
with the password from Secrets Manager, so no URL containing a password is ever stored anywhere:
  <PREFIX>_HOST, <PREFIX>_PORT (default 5432), <PREFIX>_NAME, <PREFIX>_USER, <PREFIX>_PASSWORD,
  <PREFIX>_SSLMODE (default "require" when built from parts).
Prefixes: DB (runtime role), OWNER_DB (migrations, tenant admin), MASTER_DB (one-time bootstrap).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import quote


def url_from_parts(prefix: str, env: Mapping[str, str] | None = None) -> str | None:
    e = os.environ if env is None else env
    host, name, user, password = (e.get(f"{prefix}_{k}") for k in ("HOST", "NAME", "USER", "PASSWORD"))
    if not (host and name and user and password):
        return None
    port = e.get(f"{prefix}_PORT") or "5432"
    sslmode = e.get(f"{prefix}_SSLMODE") or "require"
    return (
        f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{int(port)}/"
        f"{quote(name, safe='')}?sslmode={quote(sslmode, safe='')}"
    )


def database_url(url_var: str, prefix: str, env: Mapping[str, str] | None = None) -> str | None:
    """The full URL in `url_var` if set, else one built from `prefix`_* parts, else None."""
    e = os.environ if env is None else env
    return e.get(url_var) or url_from_parts(prefix, e)
