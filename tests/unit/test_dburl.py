"""Database URLs from injected parts (AWS) and the GitHub key source."""

from __future__ import annotations

import pytest

from aieos.core.dburl import database_url, url_from_parts
from aieos.core.settings import GitHubSettings, Settings

# Built at runtime so secret scanners do not flag a fake key in this file.
PEM_HEADER = "-----BEGIN " + "PRIVATE KEY-----"

PARTS = {"DB_HOST": "db.internal", "DB_NAME": "aieos", "DB_USER": "aieos_api", "DB_PASSWORD": "p@ss/w:rd?"}


def test_url_from_parts_quotes_the_password_and_requires_tls():
    url = url_from_parts("DB", PARTS)
    assert url == "postgresql+psycopg://aieos_api:p%40ss%2Fw%3Ard%3F@db.internal:5432/aieos?sslmode=require"
    assert url_from_parts("DB", {**PARTS, "DB_PORT": "6432", "DB_SSLMODE": "verify-full"}).endswith(
        ":6432/aieos?sslmode=verify-full"
    )


def test_missing_parts_give_none_and_a_full_url_wins():
    assert url_from_parts("DB", {"DB_HOST": "x"}) is None
    assert database_url("DATABASE_URL", "DB", {"DATABASE_URL": "postgresql+psycopg://a@b/c", **PARTS}).endswith("@b/c")
    assert database_url("OWNER_DATABASE_URL", "OWNER_DB", {}) is None


def test_settings_build_the_runtime_url_from_parts(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for k, v in PARTS.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None, oidc_issuer="https://i", oidc_audience="a", oidc_jwks_path="x.json")
    assert s.database_url.get_secret_value().startswith("postgresql+psycopg://aieos_api:p%40ss")
    assert "p@ss" not in repr(s)  # still a secret


def test_github_key_from_file_or_secret(tmp_path, monkeypatch):
    key = tmp_path / "app.pem"
    key.write_text(f"{PEM_HEADER}\nx\n")
    assert (
        GitHubSettings(_env_file=None, github_app_id="1", github_app_private_key_path=key)
        .private_key_pem()
        .startswith("-----BEGIN")
    )
    calls = []

    class FakeClient:
        def get_secret_value(self, SecretId):
            calls.append(SecretId)
            return {"SecretString": f"{PEM_HEADER}\ny\n"}

    monkeypatch.setattr("boto3.client", lambda service: FakeClient())
    s = GitHubSettings(_env_file=None, github_app_id="1", github_app_private_key_secret_arn="arn:aws:secretsmanager:x")
    assert s.configured and s.private_key_pem().endswith("y\n") and calls == ["arn:aws:secretsmanager:x"]
    assert not GitHubSettings(_env_file=None, github_app_id="1").configured


def test_bootstrap_db_requires_its_inputs(monkeypatch, capsys):
    from aieos.cli import admin

    for var in ("MASTER_DATABASE_URL", "MASTER_DB_HOST", "OWNER_DB_PASSWORD", "DB_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit):
        admin.main(["bootstrap-db"])
    assert "MASTER_DATABASE_URL" in capsys.readouterr().err
