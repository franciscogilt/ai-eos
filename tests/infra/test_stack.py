"""AWS infrastructure: synthesize the CDK stack (nothing is deployed) and check its security properties.

Needs the `infra` dependency group and Node.js:  uv sync --group infra
Skipped when aws-cdk-lib is missing, unless AIEOS_REQUIRE_CDK=1 (set in CI).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infrastructure" / "aws"))

try:
    import aws_cdk as cdk
    from aws_cdk.assertions import Match, Template
except ImportError:  # pragma: no cover
    if os.environ.get("AIEOS_REQUIRE_CDK") == "1":
        raise
    pytest.skip("aws-cdk-lib not installed (uv sync --group infra)", allow_module_level=True)

from aieos_infra.config import EnvConfig, load  # noqa: E402
from aieos_infra.stack import AieosStack  # noqa: E402

CONTEXT = {
    "github_repository": "franciscogilt/ai-eos",
    "oidc_issuer": "https://issuer.example",
    "oidc_audience": "aieos",
    "oidc_jwks_url": "https://issuer.example/.well-known/jwks.json",
    "temporal_address": "ns.acct.tmprl.cloud:7233",
    "temporal_namespace": "ns.acct",
    "github_app_id": "123",
}


def synth(**overrides: Any) -> Template:
    name = overrides.pop("name", "staging")
    first = overrides.pop("first_deploy", False)
    cfg = load(name, {**CONTEXT, **overrides})
    app = cdk.App()
    stack = AieosStack(
        app,
        f"t-{name}",
        cfg=cfg,
        image_tag="abc123",
        first_deploy=first,
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def staging() -> Template:
    return synth()


@pytest.fixture(scope="module")
def production() -> Template:
    return synth(
        name="production",
        certificate_arn="arn:aws:acm:us-east-1:111111111111:certificate/x",
        db_multi_az=True,
        create_github_oidc_provider=False,
    )


def resources(t: Template, kind: str) -> dict[str, Any]:
    return t.find_resources(kind)


def task_def(t: Template, container: str) -> dict[str, Any]:
    for res in resources(t, "AWS::ECS::TaskDefinition").values():
        defs = res["Properties"]["ContainerDefinitions"]
        if any(d["Name"] == container for d in defs):
            return res
    raise AssertionError(f"no task definition with container {container}")


def env_of(td: dict[str, Any], container: str) -> dict[str, Any]:
    d = next(d for d in td["Properties"]["ContainerDefinitions"] if d["Name"] == container)
    return {e["Name"]: e["Value"] for e in d.get("Environment", [])} | {
        s["Name"]: "<secret>" for s in d.get("Secrets", [])
    }


def policy_text_for_role(t: Template, role_logical_id_fragment: str) -> str:
    out = []
    for res in resources(t, "AWS::IAM::Policy").values():
        if role_logical_id_fragment in json.dumps(res["Properties"].get("Roles", [])):
            out.append(json.dumps(res["Properties"]["PolicyDocument"]))
    return "\n".join(out)


# ------------------------------------------------------------------ config validation
def test_placeholders_and_missing_https_are_refused():
    with pytest.raises(ValueError, match="oidc_issuer"):
        load("staging", {**CONTEXT, "oidc_issuer": "REPLACE_oidc_issuer_url"})
    with pytest.raises(ValueError, match="certificate_arn"):
        load("production", CONTEXT)
    with pytest.raises(ValueError, match="staging or production"):
        load("dev", CONTEXT)
    assert isinstance(load("staging", CONTEXT), EnvConfig)


def test_shipped_cdk_json_has_placeholders_until_configured():
    context = json.loads((ROOT / "infrastructure" / "aws" / "cdk.json").read_text())["context"]
    with pytest.raises(ValueError, match=r"set these in cdk\.json"):
        load("staging", context["aieos:staging"])


# ------------------------------------------------------------------ data protection
def test_artifact_bucket_is_private_encrypted_and_tls_only(staging):
    staging.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    Match.object_like({"ServerSideEncryptionByDefault": Match.object_like({"SSEAlgorithm": "aws:kms"})})
                ]
            },
            "VersioningConfiguration": {"Status": "Enabled"},
        },
    )
    policy = json.dumps(resources(staging, "AWS::S3::BucketPolicy"))
    assert '"aws:SecureTransport": "false"' in policy and '"Effect": "Deny"' in policy


def test_database_is_private_encrypted_and_forces_tls(staging, production):
    staging.has_resource_properties(
        "AWS::RDS::DBInstance", {"StorageEncrypted": True, "PubliclyAccessible": False, "Engine": "postgres"}
    )
    staging.has_resource_properties(
        "AWS::RDS::DBParameterGroup", {"Parameters": Match.object_like({"rds.force_ssl": "1"})}
    )
    production.has_resource_properties(
        "AWS::RDS::DBInstance", {"DeletionProtection": True, "MultiAZ": True, "BackupRetentionPeriod": 30}
    )
    production.has_resource("AWS::RDS::DBInstance", {"DeletionPolicy": "Retain"})
    staging.has_resource("AWS::RDS::DBInstance", {"DeletionPolicy": "Snapshot"})


def test_database_accepts_only_aieos_tasks(staging):
    ingress = [
        r["Properties"]
        for r in resources(staging, "AWS::EC2::SecurityGroupIngress").values()
        if r["Properties"].get("FromPort") == 5432
    ]
    assert len(ingress) == 4  # api, agents, gateway, admin tasks
    assert all("SourceSecurityGroupId" in i and "CidrIp" not in i for i in ingress)


def test_kms_key_rotates_and_survives_stack_deletion(staging):
    staging.has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})
    staging.has_resource("AWS::KMS::Key", {"DeletionPolicy": "Retain"})


def test_images_are_immutable_and_scanned(staging):
    repos = resources(staging, "AWS::ECR::Repository")
    assert len(repos) == 2  # platform, sandbox
    for repo in repos.values():
        p = repo["Properties"]
        assert p["ImageTagMutability"] == "IMMUTABLE" and p["ImageScanningConfiguration"] == {"ScanOnPush": True}


# ------------------------------------------------------------------ least privilege between services
def test_no_plaintext_secrets_in_task_definitions(staging):
    for res in resources(staging, "AWS::ECS::TaskDefinition").values():
        for d in res["Properties"]["ContainerDefinitions"]:
            for e in d.get("Environment", []):
                name = e["Name"]
                assert not name.endswith(("PASSWORD", "API_KEY", "PRIVATE_KEY")), name
                assert "BEGIN" not in json.dumps(e["Value"])


def test_only_the_gateway_holds_github_and_s3_write(staging):
    gateway = task_def(staging, "gateway")
    agents = task_def(staging, "agents")
    gw_env, ag_env = env_of(gateway, "gateway"), env_of(agents, "agents")
    assert gw_env["SANDBOX_BACKEND"] == "fargate" and "GITHUB_APP_PRIVATE_KEY_SECRET_ARN" in gw_env
    assert "GITHUB_APP_PRIVATE_KEY_SECRET_ARN" not in ag_env and "SANDBOX_BACKEND" not in ag_env
    assert "ANTHROPIC_API_KEY" in ag_env and "ANTHROPIC_API_KEY" not in gw_env  # model keys: agents only

    gateway_policy = policy_text_for_role(staging, "GatewayTaskTaskRole")
    agents_policy = policy_text_for_role(staging, "AgentsTaskTaskRole")
    assert "s3:PutObject" in gateway_policy and "secretsmanager:GetSecretValue" in gateway_policy
    assert "s3:" not in agents_policy and "GitHubAppKey" not in agents_policy


def test_sandbox_tasks_have_no_credentials_and_no_internet(staging):
    td = task_def(staging, "sandbox")
    assert "TaskRoleArn" not in td["Properties"]  # no AWS credentials inside a sandbox
    container = td["Properties"]["ContainerDefinitions"][0]
    assert container["ReadonlyRootFilesystem"] is True and container["User"] == "0"
    assert container["EntryPoint"] == ["python3", "/opt/aieos/executor.py"]
    assert "LogConfiguration" not in container  # no egress to CloudWatch needed

    sg = next(
        r["Properties"]
        for r in resources(staging, "AWS::EC2::SecurityGroup").values()
        if r["Properties"].get("GroupDescription") == "Code sandbox: no internet"
    )
    egress = [e for e in sg.get("SecurityGroupEgress", [])]
    egress += [
        r["Properties"]
        for r in resources(staging, "AWS::EC2::SecurityGroupEgress").values()
        if "SandboxSg" in json.dumps(r["Properties"].get("GroupId"))
    ]
    assert egress and all(e.get("FromPort") == 443 and "CidrIp" not in e for e in egress)
    ingress = [
        r["Properties"]
        for r in resources(staging, "AWS::EC2::SecurityGroupIngress").values()
        if "SandboxSg" in json.dumps(r["Properties"].get("GroupId"))
    ]
    assert [i["FromPort"] for i in ingress] == [7070] and "Gateway" in json.dumps(ingress[0]["SourceSecurityGroupId"])

    # Sandbox subnets are isolated: no route to a NAT or internet gateway.
    for rt in resources(staging, "AWS::EC2::Route").values():
        if "sandbox" in json.dumps(rt["Properties"]["RouteTableId"]).lower():
            raise AssertionError("sandbox subnets must not have routes out of the VPC")

    # Their S3 endpoint only serves ECR's layer bucket.
    policies = [
        json.dumps(r["Properties"].get("PolicyDocument", {}))
        for r in resources(staging, "AWS::EC2::VPCEndpoint").values()
        if r["Properties"].get("VpcEndpointType", "Gateway") == "Gateway"
    ]
    assert any("starport-layer-bucket" in p for p in policies)


def test_gateway_may_start_only_sandbox_tasks(staging):
    policy = policy_text_for_role(staging, "GatewayTaskTaskRole")
    assert "ecs:RunTask" in policy and "SandboxTask" in policy
    assert "iam:PassRole" in policy and '"Action": "*"' not in policy


def test_api_is_https_only_in_production(production, staging):
    listeners = [r["Properties"] for r in resources(production, "AWS::ElasticLoadBalancingV2::Listener").values()]
    assert {li["Protocol"] for li in listeners} == {"HTTPS", "HTTP"}
    http = next(li for li in listeners if li["Protocol"] == "HTTP")
    assert http["DefaultActions"][0]["Type"] == "redirect"
    production.has_resource_properties("AWS::ElasticLoadBalancingV2::TargetGroup", {"HealthCheckPath": "/healthz"})


# ------------------------------------------------------------------ deploy role
def test_deploy_role_trusts_only_this_repositorys_environment(staging, production):
    for template, env in ((staging, "staging"), (production, "production")):
        roles = [
            r["Properties"]
            for r in resources(template, "AWS::IAM::Role").values()
            if "GitHub Actions deploys" in r["Properties"].get("Description", "")
        ]
        assert len(roles) == 1
        trust = json.dumps(roles[0]["AssumeRolePolicyDocument"])
        assert f"repo:franciscogilt/ai-eos:environment:{env}" in trust and "StringLike" not in trust
        assert roles[0].get("MaxSessionDuration") == 3600
    policy = policy_text_for_role(staging, "DeployRole")
    assert '"Action": "*"' not in policy and "iam:CreateRole" not in policy
    assert len(resources(production, "AWS::IAM::OIDCProvider")) == 0  # reuses the account's provider
    assert len(resources(staging, "AWS::IAM::OIDCProvider")) == 1
    assert not resources(staging, "Custom::AWSCDKOpenIdConnectProvider")  # no custom-resource Lambda


def test_first_deploy_runs_no_service_tasks():
    template = synth(first_deploy=True)
    for svc in resources(template, "AWS::ECS::Service").values():
        assert svc["Properties"]["DesiredCount"] == 0
    for svc in resources(synth(), "AWS::ECS::Service").values():
        assert svc["Properties"]["DesiredCount"] >= 1
