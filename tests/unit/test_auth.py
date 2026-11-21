from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest

from aieos.api.auth import TokenVerifier, jwks_file_resolver
from aieos.cli.admin import DEV_KID, dev_keys, dev_token
from aieos.core.errors import AuthenticationError

ISS, AUD = "https://issuer.test", "aieos-api"


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    good = tmp_path_factory.mktemp("good")
    other = tmp_path_factory.mktemp("other")
    jwks = dev_keys(good)
    dev_keys(other)
    verifier = TokenVerifier(issuer=ISS, audience=AUD, algorithms=("RS256",), resolve_key=jwks_file_resolver(jwks))
    return good, other, verifier


def test_valid_token(keys):
    good, _, verifier = keys
    v = verifier.verify(dev_token(good, issuer=ISS, audience=AUD, sub="alice", email="alice@example.test"))
    assert v.sub == "alice" and v.email == "alice@example.test"


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"audience": "someone-else"}, "invalid token"),
        ({"issuer": "https://evil.test"}, "invalid token"),
        ({"ttl_seconds": -120}, "token expired"),
    ],
)
def test_rejected_claims(keys, kwargs, reason):
    good, _, verifier = keys
    params = {"issuer": ISS, "audience": AUD, "sub": "alice", **kwargs}
    with pytest.raises(AuthenticationError, match=reason):
        verifier.verify(dev_token(good, **params))


def test_wrong_signing_key(keys):
    _, other, verifier = keys
    with pytest.raises(AuthenticationError, match="invalid token"):
        verifier.verify(dev_token(other, issuer=ISS, audience=AUD, sub="alice"))


def _claims():
    now = int(time.time())
    return {"iss": ISS, "aud": AUD, "sub": "alice", "iat": now, "exp": now + 600}


def test_alg_none_rejected(keys):
    *_, verifier = keys
    token = jwt.encode(_claims(), None, algorithm="none", headers={"kid": DEV_KID})
    with pytest.raises(AuthenticationError, match="algorithm not allowed"):
        verifier.verify(token)


def test_hs256_key_confusion_rejected(keys):
    """Classic attack: HMAC-sign with the server's *public* key. Forged by hand because
    PyJWT itself refuses to produce such a token."""
    good, _, verifier = keys
    public_jwks = (good / "jwks.json").read_bytes()

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    signing_input = (
        b64(json.dumps({"alg": "HS256", "kid": DEV_KID}).encode()) + "." + b64(json.dumps(_claims()).encode())
    )
    signature = hmac.new(public_jwks, signing_input.encode(), hashlib.sha256).digest()
    token = signing_input + "." + b64(signature)
    with pytest.raises(AuthenticationError, match="algorithm not allowed"):
        verifier.verify(token)


def test_missing_sub_rejected(keys):
    good, _, verifier = keys
    claims = _claims()
    del claims["sub"]
    token = jwt.encode(claims, (good / "private.pem").read_bytes(), algorithm="RS256", headers={"kid": DEV_KID})
    with pytest.raises(AuthenticationError):
        verifier.verify(token)


def test_unknown_kid_rejected(keys):
    good, _, verifier = keys
    token = jwt.encode(_claims(), (good / "private.pem").read_bytes(), algorithm="RS256", headers={"kid": "nope"})
    with pytest.raises(AuthenticationError, match="signing key not found"):
        verifier.verify(token)


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c"])
def test_malformed(keys, garbage):
    *_, verifier = keys
    with pytest.raises(AuthenticationError):
        verifier.verify(garbage)
