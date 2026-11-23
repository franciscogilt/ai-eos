"""Per-environment settings, read from cdk.json context (`aieos:<env>`), validated before synthesis."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_PLACEHOLDER = re.compile(r"REPLACE")


@dataclass(frozen=True)
class EnvConfig:
    name: str  # staging | production
    github_repository: str  # owner/repo allowed to deploy through OIDC
    oidc_issuer: str
    oidc_audience: str
    oidc_jwks_url: str
    temporal_address: str
    temporal_namespace: str
    certificate_arn: str | None = None  # ACM certificate for the API's HTTPS listener
    github_app_id: str | None = None  # not secret; the private key is in Secrets Manager
    db_instance_class: str = "t4g.micro"
    db_multi_az: bool = False
    db_allocated_storage_gb: int = 20
    nat_gateways: int = 1
    api_desired_count: int = 1
    agents_desired_count: int = 1
    sandbox_host_instance_type: str = "t3.large"
    sandbox_host_count: int = 1
    create_github_oidc_provider: bool = True
    log_retention_days: int = 30
    model_routing_path: str = "engineering/model-routing.yaml"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def production(self) -> bool:
        return self.name == "production"


def load(name: str, context: dict[str, Any] | None) -> EnvConfig:
    if name not in ("staging", "production"):
        raise ValueError("environment must be staging or production (cdk ... -c env=staging)")
    if not context:
        raise ValueError(f"no `aieos:{name}` block in cdk.json context")
    cfg = EnvConfig(name=name, **context)
    missing = [
        k
        for k in (
            "github_repository",
            "oidc_issuer",
            "oidc_audience",
            "oidc_jwks_url",
            "temporal_address",
            "temporal_namespace",
        )
        if not getattr(cfg, k) or _PLACEHOLDER.search(str(getattr(cfg, k)))
    ]
    if missing:
        raise ValueError(f"aieos:{name}: set these in cdk.json before synthesizing: {', '.join(missing)}")
    if cfg.production and not cfg.certificate_arn:
        raise ValueError("production needs certificate_arn: the API is served over HTTPS only")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", cfg.github_repository):
        raise ValueError("github_repository must be owner/repo")
    return cfg
