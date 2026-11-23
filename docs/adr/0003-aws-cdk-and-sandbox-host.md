# ADR-0003: AWS infrastructure as CDK (Python); sandboxes on a dedicated EC2 host

- Status: accepted (code only; nothing deployed)
- Date: 2026-11-23

## Context
The platform needs an AWS deployment: API, agent worker, tool gateway, PostgreSQL, S3, Temporal Cloud
and code sandboxes. Sandboxes are Docker containers that the gateway starts through a Docker socket
(Increment 5), with no network, a read-only root and resource limits. Fargate offers no Docker socket,
and a Fargate-native sandbox backend does not exist yet.

## Decision
- **Infrastructure tool:** AWS CDK in Python (`infrastructure/aws`).
  - It uses the platform's language and its tests run in CI (`tests/infra`) without an AWS account.
  - It synthesizes plain CloudFormation.
  - Its dependencies live in an `infra` group that never enters the platform images.
- **One stack per environment** (`aieos-staging`, `aieos-production`), configured in `cdk.json`.
  Placeholders are refused at synthesis.
- **The tool-gateway worker runs on a dedicated EC2 sandbox host** (an ECS capacity provider), with
  the host's Docker socket mounted:
  - The host has no inbound traffic, IMDSv2 required with a hop limit of 1, and encrypted volumes.
  - The gateway is the only task with the GitHub App key (read from Secrets Manager at startup) and
    S3 write access.
  - The agents worker has the model keys but no S3 and no GitHub access.
- **API on Fargate** behind a public ALB, HTTPS-only in production.
- **Agent worker on Fargate**, with no inbound traffic.
- **Database credentials never appear as URLs.**
  - ECS injects host, user and password separately (password from Secrets Manager).
  - The application builds the URL with `sslmode=require`.
  - RDS forces TLS.
  - A one-off `aieos-admin bootstrap-db` task creates the owner and runtime roles as the RDS master.
- **Deploys** use a GitHub Actions OIDC role trusted only for `repo:<owner>/<repo>:environment:<env>`.
  The workflow is manual and runs in a protected GitHub environment; no AWS keys are stored.

## Consequences
- **Root-equivalent gateway.** The Docker socket makes the gateway root-equivalent on its host. That host
  therefore runs only the gateway and its sandboxes: the host is dedicated, and sandboxes have
  `--network none` and dropped capabilities.
- **Next step for isolation:** a sandbox backend on Fargate or Firecracker (no shared kernel with the
  gateway). The gateway interface stays the same.
- **No live deploy yet.** Nothing has been deployed. Tests synthesize the template and check its
  security properties; a real deploy needs an AWS account, an authorization decision by the owner and the
  values in `cdk.json`.
