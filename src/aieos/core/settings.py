"""Process configuration from environment variables (and `.env` locally).

Fails fast: a missing required value or an unsafe combination raises at startup,
never at first use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    environment: Environment = "local"

    # Runtime database connection: a member of aieos_app, never the table owner.
    database_url: SecretStr
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_statement_timeout_ms: int = Field(default=15_000, ge=100, le=300_000)

    # OIDC token verification. Exactly one key source: a JWKS URL, or (local/test only) a JWKS file.
    oidc_issuer: str = Field(min_length=1)
    oidc_audience: str = Field(min_length=1)
    oidc_jwks_url: AnyHttpUrl | None = None
    oidc_jwks_path: Path | None = None
    oidc_algorithms: tuple[str, ...] = ("RS256", "ES256")

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65_535)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "aieos-api"

    # "disabled" serves everything except workflow start/signal routes (503). Tests and local API-only runs.
    orchestrator_mode: Literal["temporal", "disabled"] = "temporal"

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if (self.oidc_jwks_url is None) == (self.oidc_jwks_path is None):
            raise ValueError("set exactly one of OIDC_JWKS_URL or OIDC_JWKS_PATH")
        if self.oidc_jwks_path is not None and self.environment not in ("local", "test"):
            raise ValueError("OIDC_JWKS_PATH is only allowed when ENVIRONMENT is local or test")
        if self.environment in ("staging", "production"):
            if not self.oidc_issuer.startswith("https://"):
                raise ValueError("OIDC_ISSUER must use https outside local/test")
            if self.oidc_jwks_url is not None and self.oidc_jwks_url.scheme != "https":
                raise ValueError("OIDC_JWKS_URL must use https outside local/test")
        if not self.oidc_algorithms:
            raise ValueError("OIDC_ALGORITHMS must not be empty")
        disallowed = [a for a in self.oidc_algorithms if a.lower() == "none" or a.upper().startswith("HS")]
        if disallowed:
            # Symmetric algorithms would let anyone holding the public JWKS forge tokens.
            raise ValueError(f"disallowed JWT algorithms: {disallowed}")
        return self


class StorageSettings(BaseSettings):
    """Artifact storage. In AWS, credentials come from the task role (no keys here)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    s3_artifact_bucket: str = Field(min_length=3, max_length=63)
    s3_region: str = "us-east-1"
    s3_endpoint_url: str | None = None  # local S3-compatible store only
    s3_force_path_style: bool = False
    s3_kms_key_id: str | None = None  # SSE-KMS key; bucket default encryption applies when unset
    # Static keys for the local S3-compatible store. Leave unset in AWS.
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    artifact_max_bytes: int = Field(default=100 * 1024 * 1024, gt=0, le=5 * 1024**3)
    presign_ttl_seconds: int = Field(default=300, ge=1, le=900)


class ModelRoutingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    model_routing_path: Path = Path("engineering/model-routing.yaml")


class TemporalSettings(BaseSettings):
    """Temporal connection. Local: the dev server. AWS: Temporal Cloud with an API key."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "aieos-local"
    temporal_api_key: SecretStr | None = None
    temporal_tls: bool = False


class SandboxSettings(BaseSettings):
    """Code-execution sandboxes. `docker` is for local development and CI; AWS uses Fargate (later)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    sandbox_backend: Literal["disabled", "docker"] = "disabled"
    sandbox_image: str = "aieos-sandbox:local"
    sandbox_runtime: str | None = None  # e.g. "runsc" for gVisor, where installed
    sandbox_default_cpus: float = Field(default=2.0, gt=0, le=16)
    sandbox_default_memory_mb: int = Field(default=2048, ge=256, le=32768)
    sandbox_default_timeout_seconds: int = Field(default=900, ge=10, le=3600)
    sandbox_max_age_seconds: int = Field(default=6 * 3600, ge=600)


class GitHubSettings(BaseSettings):
    """GitHub App credentials, read only by the tool-gateway worker. The private key is a file path
    (mounted secret), never an inline value, so it cannot leak through environment dumps."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    github_app_id: str | None = None
    github_app_private_key_path: Path | None = None
    github_api_url: str = "https://api.github.com"

    @property
    def configured(self) -> bool:
        return bool(self.github_app_id and self.github_app_private_key_path)
