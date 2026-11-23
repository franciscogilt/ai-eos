"""AI-EOS on AWS: one stack per environment.

Network     VPC over 2 AZs: public (ALB, NAT), private with egress (services), isolated (RDS).
Data        RDS PostgreSQL 16 (encrypted, TLS forced, isolated subnets); S3 artifact bucket (KMS,
            TLS only, no public access, versioned). One customer-managed KMS key with rotation.
Compute     ECS cluster.
            - api: Fargate behind a public ALB (HTTPS when a certificate is configured).
            - agents worker: Fargate, no inbound traffic, no S3 and no GitHub access (it reaches tools
              only through the gateway, via Temporal).
            - gateway worker: on a dedicated EC2 sandbox host, because sandboxes are Docker containers
              (network none, read-only root, resource limits) started through the host's Docker socket.
              It alone holds the GitHub App key and S3 write access.
            - one-off tasks: bootstrap-db, migrate, publish-agents.
Secrets     Secrets Manager: RDS master (generated), owner and runtime DB passwords (generated), and
            empty placeholders for the GitHub App key, Temporal Cloud API key and model API keys that
            an operator fills in. No secret is ever passed as plain text in a task definition.
Deploy      GitHub Actions OIDC role, limited to this repository's `<env>` environment.

Synthesizing creates nothing. Deploying is a separate, deliberate `cdk deploy` (see README).
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_autoscaling as autoscaling
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ecs_patterns as ecs_patterns
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from aieos_infra.config import EnvConfig

DB_NAME = "aieos"
SANDBOX_IMAGE_TAG = "latest"  # the deploy workflow passes an immutable tag via the image_tag context


class AieosStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        image_tag: str,
        first_deploy: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.cfg = cfg
        # First deploy: create everything but run no service tasks (their images are not pushed yet).
        running = 0 if first_deploy else None
        retain = RemovalPolicy.RETAIN if cfg.production else RemovalPolicy.SNAPSHOT

        # ------------------------------------------------------------------ network
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=cfg.nat_gateways,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="private", subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, cidr_mask=22),
                ec2.SubnetConfiguration(name="data", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
            ],
        )
        self.vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)
        self.vpc.add_flow_log("FlowLog", traffic_type=ec2.FlowLogTrafficType.REJECT)

        # ------------------------------------------------------------------ encryption and storage
        # Never deleted with the stack: it protects backups and retained data.
        self.key = kms.Key(
            self,
            "DataKey",
            enable_key_rotation=True,
            alias=f"alias/aieos-{cfg.name}",
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.bucket = s3.Bucket(
            self,
            "Artifacts",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[s3.LifecycleRule(noncurrent_version_expiration=Duration.days(30))],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------ database
        self.db_sg = ec2.SecurityGroup(
            self, "DbSg", vpc=self.vpc, allow_all_outbound=False, description="PostgreSQL: only from AI-EOS tasks"
        )
        params = rds.ParameterGroup(
            self,
            "DbParams",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_16),
            parameters={"rds.force_ssl": "1", "log_min_duration_statement": "2000"},
        )
        self.db = rds.DatabaseInstance(
            self,
            "Db",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_16),
            instance_type=ec2.InstanceType(cfg.db_instance_class),
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[self.db_sg],
            credentials=rds.Credentials.from_generated_secret("aieos_master", encryption_key=self.key),
            allocated_storage=cfg.db_allocated_storage_gb,
            max_allocated_storage=cfg.db_allocated_storage_gb * 5,
            storage_encrypted=True,
            storage_encryption_key=self.key,
            multi_az=cfg.db_multi_az,
            publicly_accessible=False,
            backup_retention=Duration.days(30 if cfg.production else 7),
            deletion_protection=cfg.production,
            parameter_group=params,
            cloudwatch_logs_exports=["postgresql"],
            auto_minor_version_upgrade=True,
            removal_policy=retain,
        )

        def generated(name: str, description: str) -> secretsmanager.Secret:
            return secretsmanager.Secret(
                self,
                name,
                description=description,
                encryption_key=self.key,
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    exclude_punctuation=True, password_length=40
                ),
            )

        def operator_filled(name: str, description: str) -> secretsmanager.Secret:
            # Created with a random placeholder; an operator replaces the value (never from code).
            return secretsmanager.Secret(self, name, description=description, encryption_key=self.key)

        self.owner_pw = generated("DbOwnerPassword", "aieos_owner: migrations and tenant administration")
        self.api_pw = generated("DbApiPassword", "aieos_api: runtime role (member of aieos_app, subject to RLS)")
        self.github_key = operator_filled("GitHubAppKey", "GitHub App private key (PEM), read by the gateway only")
        self.temporal_key = operator_filled("TemporalApiKey", "Temporal Cloud API key")
        self.model_keys = operator_filled(
            "ModelApiKeys", 'JSON: {"ANTHROPIC_API_KEY": "...", "GEMINI_API_KEY": "...", "OPENAI_API_KEY": "..."}'
        )

        # ------------------------------------------------------------------ images and cluster
        def repository(name: str) -> ecr.Repository:
            return ecr.Repository(
                self,
                name,
                image_scan_on_push=True,
                image_tag_mutability=ecr.TagMutability.IMMUTABLE,
                encryption=ecr.RepositoryEncryption.KMS,
                encryption_key=self.key,
                lifecycle_rules=[ecr.LifecycleRule(max_image_count=30)],
                removal_policy=RemovalPolicy.RETAIN,
            )

        self.platform_repo = repository("PlatformRepo")
        self.gateway_repo = repository("GatewayRepo")
        self.sandbox_repo = repository("SandboxRepo")
        platform_image = ecs.ContainerImage.from_ecr_repository(self.platform_repo, image_tag)
        gateway_image = ecs.ContainerImage.from_ecr_repository(self.gateway_repo, image_tag)

        self.cluster = ecs.Cluster(self, "Cluster", vpc=self.vpc, container_insights_v2=ecs.ContainerInsights.ENABLED)
        self.log_group = logs.LogGroup(
            self,
            "Logs",
            retention=_retention(cfg.log_retention_days),
            removal_policy=RemovalPolicy.RETAIN if cfg.production else RemovalPolicy.DESTROY,
        )

        common_env = {
            "ENVIRONMENT": cfg.name,
            "LOG_JSON": "true",
            "OIDC_ISSUER": cfg.oidc_issuer,
            "OIDC_AUDIENCE": cfg.oidc_audience,
            "OIDC_JWKS_URL": cfg.oidc_jwks_url,
            "TEMPORAL_ADDRESS": cfg.temporal_address,
            "TEMPORAL_NAMESPACE": cfg.temporal_namespace,
            "TEMPORAL_TLS": "true",
            "S3_ARTIFACT_BUCKET": self.bucket.bucket_name,
            "S3_REGION": self.region,
            "S3_KMS_KEY_ID": self.key.key_arn,
            "MODEL_ROUTING_PATH": cfg.model_routing_path,
            "DB_HOST": self.db.db_instance_endpoint_address,
            "DB_PORT": self.db.db_instance_endpoint_port,
            "DB_NAME": DB_NAME,
            "DB_USER": "aieos_api",
        }
        runtime_secrets = {
            "DB_PASSWORD": ecs.Secret.from_secrets_manager(self.api_pw),
            "TEMPORAL_API_KEY": ecs.Secret.from_secrets_manager(self.temporal_key),
        }
        model_secrets = {
            name: ecs.Secret.from_secrets_manager(self.model_keys, name)
            for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY")
        }

        # ------------------------------------------------------------------ api (public, behind the ALB)
        certificate = (
            acm.Certificate.from_certificate_arn(self, "ApiCert", cfg.certificate_arn) if cfg.certificate_arn else None
        )
        api = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "Api",
            cluster=self.cluster,
            desired_count=cfg.api_desired_count,
            cpu=512,
            memory_limit_mib=1024,
            public_load_balancer=True,
            certificate=certificate,
            redirect_http=certificate is not None,
            protocol=elbv2.ApplicationProtocol.HTTPS if certificate else elbv2.ApplicationProtocol.HTTP,
            task_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=platform_image,
                container_port=8000,
                environment={**common_env, "OTEL_SERVICE_NAME": "aieos-api"},
                secrets=runtime_secrets,
                log_driver=ecs.LogDrivers.aws_logs(stream_prefix="api", log_group=self.log_group),
            ),
        )
        if running is not None:  # the pattern refuses 0; set it on the underlying resource instead
            api.service.node.default_child.add_property_override("DesiredCount", running)  # type: ignore[union-attr]
        api.target_group.configure_health_check(path="/healthz", healthy_http_codes="200")
        api.load_balancer.set_attribute("routing.http.drop_invalid_header_fields.enabled", "true")
        self.bucket.grant_read(api.task_definition.task_role)  # downloads via presigned URLs
        self._db_access(api.service.connections)
        self.api = api

        # ------------------------------------------------------------------ agents worker (no inbound)
        agents_td = ecs.FargateTaskDefinition(self, "AgentsTask", cpu=1024, memory_limit_mib=2048)
        agents_td.add_container(
            "agents",
            image=platform_image,
            entry_point=["aieos-worker"],
            command=["agents"],
            environment={**common_env, "OTEL_SERVICE_NAME": "aieos-agents"},
            secrets={**runtime_secrets, **model_secrets},
            logging=ecs.LogDrivers.aws_logs(stream_prefix="agents", log_group=self.log_group),
            readonly_root_filesystem=True,
        )
        self.agents = ecs.FargateService(
            self,
            "Agents",
            cluster=self.cluster,
            task_definition=agents_td,
            desired_count=cfg.agents_desired_count if running is None else running,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )
        self._db_access(self.agents.connections)

        # ------------------------------------------------------------------ gateway worker on the sandbox host
        host_sg = ec2.SecurityGroup(self, "SandboxHostSg", vpc=self.vpc, description="Sandbox host: no inbound")
        launch_template = ec2.LaunchTemplate(
            self,
            "SandboxHostTemplate",
            instance_type=ec2.InstanceType(cfg.sandbox_host_instance_type),
            machine_image=ecs.EcsOptimizedImage.amazon_linux2023(),
            security_group=host_sg,
            role=iam.Role(self, "SandboxHostRole", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com")),
            require_imdsv2=True,
            http_put_response_hop_limit=1,  # containers cannot reach the instance credentials
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(60, encrypted=True, volume_type=ec2.EbsDeviceVolumeType.GP3),
                )
            ],
            user_data=ec2.UserData.for_linux(),
        )
        asg = autoscaling.AutoScalingGroup(
            self,
            "SandboxHosts",
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            launch_template=launch_template,
            min_capacity=cfg.sandbox_host_count,
            max_capacity=cfg.sandbox_host_count,
        )
        capacity = ecs.AsgCapacityProvider(self, "SandboxCapacity", auto_scaling_group=asg)
        self.cluster.add_asg_capacity_provider(capacity)

        gateway_td = ecs.Ec2TaskDefinition(self, "GatewayTask", network_mode=ecs.NetworkMode.AWS_VPC)
        gateway_td.add_volume(name="docker-socket", host=ecs.Host(source_path="/var/run/docker.sock"))
        gateway = gateway_td.add_container(
            "gateway",
            image=gateway_image,
            memory_reservation_mib=512,
            command=["gateway"],
            environment={
                **common_env,
                "OTEL_SERVICE_NAME": "aieos-gateway",
                "SANDBOX_BACKEND": "docker",
                "SANDBOX_IMAGE": self.sandbox_repo.repository_uri_for_tag(image_tag),
                "GITHUB_APP_PRIVATE_KEY_SECRET_ARN": self.github_key.secret_arn,
                **({"GITHUB_APP_ID": cfg.github_app_id} if cfg.github_app_id else {}),
            },
            secrets=runtime_secrets,
            logging=ecs.LogDrivers.aws_logs(stream_prefix="gateway", log_group=self.log_group),
        )
        gateway.add_mount_points(
            ecs.MountPoint(source_volume="docker-socket", container_path="/var/run/docker.sock", read_only=False)
        )
        self.github_key.grant_read(gateway_td.task_role)
        self.bucket.grant_read_write(gateway_td.task_role)
        self.sandbox_repo.grant_pull(gateway_td.task_role)
        self.gateway = ecs.Ec2Service(
            self,
            "Gateway",
            cluster=self.cluster,
            task_definition=gateway_td,
            desired_count=1 if running is None else running,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            capacity_provider_strategies=[
                ecs.CapacityProviderStrategy(capacity_provider=capacity.capacity_provider_name, weight=1)
            ],
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
        )
        self._db_access(self.gateway.connections)

        # ------------------------------------------------------------------ one-off tasks (run by the deploy workflow)
        self.admin_sg = ec2.SecurityGroup(self, "AdminTaskSg", vpc=self.vpc, description="One-off admin tasks")
        self._db_access(ec2.Connections(security_groups=[self.admin_sg]))
        self.admin_td = ecs.FargateTaskDefinition(self, "AdminTask", cpu=256, memory_limit_mib=512)
        self.admin_td.add_container(
            "admin",
            image=platform_image,
            entry_point=["sh", "-c"],
            command=["echo 'override the command: bootstrap-db | migrate | publish-agents'"],
            environment={
                "MASTER_DB_HOST": self.db.db_instance_endpoint_address,
                "MASTER_DB_NAME": "postgres",
                "OWNER_DB_HOST": self.db.db_instance_endpoint_address,
                "OWNER_DB_NAME": DB_NAME,
                "OWNER_DB_USER": "aieos_owner",
            },
            secrets={
                "MASTER_DB_USER": ecs.Secret.from_secrets_manager(self.db.secret, "username"),  # type: ignore[arg-type]
                "MASTER_DB_PASSWORD": ecs.Secret.from_secrets_manager(self.db.secret, "password"),  # type: ignore[arg-type]
                "OWNER_DB_PASSWORD": ecs.Secret.from_secrets_manager(self.owner_pw),
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(self.api_pw),
            },
            logging=ecs.LogDrivers.aws_logs(stream_prefix="admin", log_group=self.log_group),
        )

        # ------------------------------------------------------------------ GitHub Actions deploy role (OIDC)
        self.deploy_role = self._deploy_role(cfg)

        for name, value in {
            "ApiUrl": f"{'https' if certificate else 'http'}://{api.load_balancer.load_balancer_dns_name}",
            "ClusterName": self.cluster.cluster_name,
            "PlatformRepo": self.platform_repo.repository_uri,
            "GatewayRepo": self.gateway_repo.repository_uri,
            "SandboxRepo": self.sandbox_repo.repository_uri,
            "AdminTaskDefinition": self.admin_td.task_definition_arn,
            "AdminSecurityGroup": self.admin_sg.security_group_id,
            "PrivateSubnets": ",".join(s.subnet_id for s in self.vpc.private_subnets),
            "DeployRoleArn": self.deploy_role.role_arn,
            "GitHubAppKeySecret": self.github_key.secret_arn,
            "TemporalApiKeySecret": self.temporal_key.secret_arn,
            "ModelApiKeysSecret": self.model_keys.secret_arn,
        }.items():
            CfnOutput(self, f"{name}Output", value=value)

    def _db_access(self, connections: ec2.Connections) -> None:
        self.db_sg.connections.allow_from(connections, ec2.Port.tcp(5432), "AI-EOS task to PostgreSQL")

    def _deploy_role(self, cfg: EnvConfig) -> iam.Role:
        issuer = "token.actions.githubusercontent.com"
        if cfg.create_github_oidc_provider:
            # Native AWS::IAM::OIDCProvider (no custom-resource Lambda).
            provider_arn = iam.OidcProviderNative(
                self, "GitHubOidc", url=f"https://{issuer}", client_ids=["sts.amazonaws.com"]
            ).oidc_provider_arn
        else:  # one provider per account: reuse the one another environment created
            provider_arn = f"arn:aws:iam::{self.account}:oidc-provider/{issuer}"
        role = iam.Role(
            self,
            "DeployRole",
            description=f"GitHub Actions deploys of {cfg.github_repository} ({cfg.name} environment only)",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                provider_arn,
                conditions={
                    "StringEquals": {
                        f"{issuer}:aud": "sts.amazonaws.com",
                        # Only jobs that run in this repository's protected GitHub environment.
                        f"{issuer}:sub": f"repo:{cfg.github_repository}:environment:{cfg.name}",
                    }
                },
            ),
        )
        for repo in (self.platform_repo, self.gateway_repo, self.sandbox_repo):
            repo.grant_pull_push(role)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeCdkDeployRoles",
                actions=["sts:AssumeRole"],
                resources=[f"arn:aws:iam::{self.account}:role/cdk-hnb659fds-*-{self.account}-{self.region}"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadStackOutputs",
                actions=["cloudformation:DescribeStacks"],
                resources=[self.stack_id],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RunOneOffAdminTasks",
                actions=["ecs:RunTask"],
                resources=[self.admin_td.task_definition_arn],
                conditions={"ArnEquals": {"ecs:cluster": self.cluster.cluster_arn}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="WatchAdminTasks",
                actions=["ecs:DescribeTasks"],
                resources=["*"],
                conditions={"ArnEquals": {"ecs:cluster": self.cluster.cluster_arn}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PassAdminTaskRoles",
                actions=["iam:PassRole"],
                resources=[self.admin_td.task_role.role_arn, self.admin_td.obtain_execution_role().role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadAdminTaskLogs",
                actions=["logs:GetLogEvents", "logs:FilterLogEvents"],
                resources=[self.log_group.log_group_arn, f"{self.log_group.log_group_arn}:*"],
            )
        )
        return role


def _retention(days: int) -> logs.RetentionDays:
    """The shortest supported CloudWatch retention that keeps at least `days`."""
    options = [
        (7, logs.RetentionDays.ONE_WEEK),
        (14, logs.RetentionDays.TWO_WEEKS),
        (30, logs.RetentionDays.ONE_MONTH),
        (60, logs.RetentionDays.TWO_MONTHS),
        (90, logs.RetentionDays.THREE_MONTHS),
        (180, logs.RetentionDays.SIX_MONTHS),
        (365, logs.RetentionDays.ONE_YEAR),
    ]
    return next((r for d, r in options if d >= days), logs.RetentionDays.TWO_YEARS)
