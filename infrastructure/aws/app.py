"""CDK entry point.  cdk synth -c env=staging   (from infrastructure/aws; see README.md)."""

from __future__ import annotations

import os

import aws_cdk as cdk
from aieos_infra.config import load
from aieos_infra.stack import AieosStack

app = cdk.App()
env_name = app.node.try_get_context("env") or "staging"
cfg = load(env_name, app.node.try_get_context(f"aieos:{env_name}"))
AieosStack(
    app,
    f"aieos-{cfg.name}",
    cfg=cfg,
    image_tag=app.node.try_get_context("image_tag") or "bootstrap",
    first_deploy=str(app.node.try_get_context("first_deploy")).lower() == "true",
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=os.environ.get("CDK_DEFAULT_REGION")),
    termination_protection=cfg.production,
)
cdk.Tags.of(app).add("project", "aieos")
cdk.Tags.of(app).add("environment", cfg.name)
app.synth()
