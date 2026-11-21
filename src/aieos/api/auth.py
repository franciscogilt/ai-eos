"""OIDC bearer-token verification.

Asymmetric algorithms only; the allowed list is checked against the token header
before any key lookup, so `alg: none` and HS256-with-public-key confusion are
rejected up front. iss, aud, exp, iat and sub are mandatory.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from jwt import PyJWKClient, PyJWKSet

from aieos.core.errors import AuthenticationError

KeyResolver = Callable[[str, dict[str, Any]], Any]

LEEWAY_SECONDS = 30


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    sub: str
    email: str | None
    name: str | None
    claims: dict[str, Any]


class TokenVerifier:
    def __init__(self, *, issuer: str, audience: str, algorithms: tuple[str, ...], resolve_key: KeyResolver):
        self._issuer = issuer
        self._audience = audience
        self._algorithms = algorithms
        self._resolve_key = resolve_key

    def verify(self, token: str) -> VerifiedToken:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthenticationError("malformed token") from None
        if header.get("alg") not in self._algorithms:
            raise AuthenticationError("token algorithm not allowed")
        key = self._resolve_key(token, header)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise AuthenticationError("token expired") from None
        except jwt.PyJWTError:
            raise AuthenticationError("invalid token") from None
        sub = claims["sub"]
        if not isinstance(sub, str) or not sub or len(sub) > 255:
            raise AuthenticationError("invalid subject")
        email = claims.get("email")
        name = claims.get("name")
        return VerifiedToken(
            sub=sub,
            email=email if isinstance(email, str) and "@" in email else None,
            name=name if isinstance(name, str) else None,
            claims=claims,
        )


def jwks_url_resolver(url: str) -> KeyResolver:
    """Remote JWKS with key caching (PyJWKClient refetches on unknown kid)."""
    client = PyJWKClient(url, cache_keys=True, lifespan=300, timeout=5)

    def resolve(token: str, _header: dict[str, Any]) -> Any:
        try:
            return client.get_signing_key_from_jwt(token).key
        except jwt.PyJWTError:
            raise AuthenticationError("signing key not found") from None

    return resolve


def jwks_file_resolver(path: Path) -> KeyResolver:
    """Local JWKS file for local/test environments (see `aieos-admin dev-keys`)."""
    keyset = PyJWKSet.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def resolve(_token: str, header: dict[str, Any]) -> Any:
        kid = header.get("kid")
        for key in keyset.keys:
            if key.key_id == kid:
                return key.key
        raise AuthenticationError("signing key not found")

    return resolve
