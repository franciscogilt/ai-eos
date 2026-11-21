from __future__ import annotations

import io
import json
import time

import pytest
from pydantic import SecretStr, ValidationError

from aieos.core.ids import uuid7
from aieos.core.logging import REDACTED, configure_logging, get_logger, redact_mapping, redact_text
from aieos.core.settings import Settings

BASE = {
    "database_url": "postgresql+psycopg://u:p@localhost/db",
    "oidc_issuer": "https://issuer.example",
    "oidc_audience": "aieos-api",
    "oidc_jwks_url": "https://issuer.example/jwks.json",
    "_env_file": None,
}


def test_uuid7_version_variant_and_order():
    a = uuid7()
    time.sleep(0.002)
    b = uuid7()
    assert a.version == 7 and a.variant == "specified in RFC 4122"
    assert a < b
    assert len({uuid7() for _ in range(1000)}) == 1000


def test_settings_valid():
    s = Settings(**BASE)
    assert s.oidc_algorithms == ("RS256", "ES256")


def test_settings_missing_required_fails_fast(monkeypatch):
    for var in ("DATABASE_URL", "OIDC_ISSUER", "OIDC_AUDIENCE"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "override",
    [
        {"oidc_algorithms": ("HS256",)},
        {"oidc_algorithms": ("none",)},
        {"oidc_jwks_url": None},  # no key source
        {"oidc_jwks_path": "/tmp/jwks.json"},  # two key sources
        {"environment": "production", "oidc_issuer": "http://issuer"},  # plain http in prod
        {"environment": "staging", "oidc_jwks_url": None, "oidc_jwks_path": "/tmp/j.json"},  # file keys outside local
    ],
)
def test_settings_rejects_unsafe(override):
    with pytest.raises(ValidationError):
        Settings(**{**BASE, **override})


@pytest.mark.parametrize(
    "secret",
    [
        "Bearer abc.def.ghi",
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl",
        "ghp_" + "a" * 36,
        "github_pat_" + "b" * 40,
        "sk-" + "c" * 32,
        "AKIA" + "D" * 16,
        "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----",
    ],
)
def test_redact_text_patterns(secret):
    out = redact_text(f"value={secret} end")
    assert secret not in out and "end" in out


def test_redact_connection_string_password():
    assert redact_text("postgresql://aieos:hunter2@db:5432/x") == "postgresql://aieos:[REDACTED]@db:5432/x"


def test_redact_mapping_by_key_keeps_counts():
    out = redact_mapping(
        {
            "password": "x",
            "api_key": "y",
            "nested": {"Authorization": "z"},
            "output_tokens": 42,
            "secret_ref": SecretStr("s"),
            "ok": "fine",
        }
    )
    assert out["password"] == out["api_key"] == out["nested"]["Authorization"] == REDACTED
    assert out["output_tokens"] == 42 and out["ok"] == "fine" and out["secret_ref"] == REDACTED


def test_logger_output_is_redacted():
    buf = io.StringIO()
    configure_logging(level="INFO", json=True, stream=buf)
    get_logger("t").info("calling github", token="ghs_" + "x" * 36, url="https://u:pw@host/x")
    record = json.loads(buf.getvalue())
    assert record["token"] == REDACTED and "pw@" not in record["url"]
