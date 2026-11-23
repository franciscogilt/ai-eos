# Deploying AI-EOS to AWS

Code only so far: nothing has been created in any AWS account. Every step below is something **you**
run, after deciding to.

## What gets created (per environment)
- **Network:** a VPC across 2 AZs with NAT, an S3 gateway endpoint and flow logs (rejected traffic).
- **Encryption:** one KMS key with rotation, used for S3, RDS, ECR, secrets and logs.
- **Data:**
  - RDS PostgreSQL 16 in isolated subnets, encrypted, with TLS forced.
  - Backups are kept 7 days in staging and 30 days in production; production also has deletion
    protection and Multi-AZ.
  - An S3 artifact bucket: private, TLS only, versioned, KMS.
- **ECR repositories:** platform, gateway and sandbox images, with immutable tags and scan on push.
- **ECS:**
  - API on Fargate behind an ALB (HTTPS with a certificate);
  - agent worker on Fargate;
  - tool gateway on a dedicated EC2 sandbox host;
  - a one-off admin task.
- **Secrets Manager:**
  - generated: RDS master, owner and runtime DB passwords;
  - placeholders you fill in: GitHub App key, Temporal Cloud API key, model API keys.
- **GitHub OIDC deploy role:** only this repository's `<env>` GitHub environment can use it.

These resources cost money while they exist, even when idle: NAT gateway, RDS, ALB and the EC2 sandbox
host. Check current AWS prices for your region before deploying.

## First deploy (by hand, with admin credentials)
1. Fill `infrastructure/aws/cdk.json` → `aieos:staging`:
   - your OIDC issuer, audience and JWKS URL;
   - your Temporal Cloud address and namespace;
   - optionally `certificate_arn` and `github_app_id`.
2. Install and log in:
   - `uv sync --group infra`, plus Node.js;
   - log in to AWS with an admin profile.
3. Bootstrap CDK in the account and region:
   `cd infrastructure/aws && npx aws-cdk@2 bootstrap`.
4. Create the stack without running services, because their images do not exist yet:
   `npx aws-cdk@2 deploy aieos-staging -c env=staging -c first_deploy=true`.
5. Fill the placeholder secrets in Secrets Manager. The stack outputs list their ARNs.
   - `GitHubAppKey`: the PEM.
   - `TemporalApiKey`.
   - `ModelApiKeys`: JSON with the keys you use.
6. In GitHub → Settings → Environments → `staging`:
   - add required reviewers;
   - set the variables `AWS_REGION` and `AWS_DEPLOY_ROLE_ARN` (the `DeployRoleArn` output).

## Every deploy after that
Actions → **deploy** → choose the environment. On the first run, tick `bootstrap_db`. The workflow:
1. builds and pushes the three images, tagged with the commit SHA;
2. runs `cdk deploy` with that tag;
3. runs the admin task: `bootstrap-db` (first time only), `alembic upgrade head`, then `publish-agents`.

## Not covered yet
- **Sandbox isolation.** The gateway uses the sandbox host's Docker socket (see ADR-0003); a
  Fargate/Firecracker sandbox backend is the follow-up.
- **Observability.** CloudWatch Logs and Container Insights only. Setting an OTLP endpoint for traces is
  left to you.
- **Testing.** `bootstrap-db` is exercised only by its argument checks in tests. Run it against a staging
  RDS before production.
