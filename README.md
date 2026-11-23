# AI-EOS

AI-driven software engineering framework and autonomous development platform.
A user connects a GitHub repository, submits a requirement, and the platform
delivers a reviewed pull request through supervised, specialised agents, with
human approval at sensitive steps and a complete audit trail.

**Status:** Increment 3 complete. On top of the API, artifact store and metered model
router: the Temporal `FeatureDeliveryWorkflow` (requirements → clarification →
architecture → plan → develop/review per task → PR → merge approval), the tool-gateway
worker that authorizes every tool call, and human approvals through the API and CLI.
**B14:** the Requirements and Architecture agents run for real (our own provider-neutral agent loop
over Anthropic, Gemini, OpenAI or any OpenAI-compatible server; metered, grounding-checked; ADR-0002). **B13:** disposable Docker sandboxes, repository snapshots through the GitHub App, read-only repo
tools, and a validated push path to `aieos/<workflow>` branches (tested live against a playground repo).
**B15:** Developer and QA/Security agents run for real. Tests, patches and security scans are produced by
the platform, never taken from a model's word; tasks stack and are pushed as one commit, and the PR is merged only at the exact commit a human approved. See `docs/blueprint/`.

### Evaluating agents
`aieos-eval --agent requirements --cases engineering/evals/requirements --max-usd 2` runs the
golden set against the configured real models with a hard spending cap (also available as the
manual `agent-evals` GitHub Actions workflow).

### Before any agent can call a model
**AWS:** infrastructure code and a manual deploy workflow are in `infrastructure/aws` (nothing deployed yet); see `docs/aws.md`.

**Tracing:** API, agent worker and gateway export OpenTelemetry traces (one trace per request across Temporal, with tokens and cost per model call; no prompts or tool content); see `docs/observability.md`.

**No API bills:** set `MODEL_ROUTING_PATH=engineering/model-routing.local.yaml` to use the coding CLIs you are
already signed in to (Claude Code, Gemini CLI, Codex) or Ollama. See `docs/local-models.md`.

With API keys instead:
Fill in `engineering/model-routing.yaml`: a model id per provider and profile under
`models`, and a price for each of those models under `pricing` (copied from the
provider's current price list). Unpriced or placeholder models are refused, so a
budget can never be bypassed. Then set the API key of each provider you use (`ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, `OPENAI_API_KEY`, or a local OpenAI-compatible server) in `.env`. No provider is
required; agent contracts name a provider slot and the routing file decides what sits behind it.

## Layout

| Path | Contents |
| --- | --- |
| `src/aieos/` | Single Python distribution, modular monolith (boundaries enforced by import-linter) |
| `src/aieos/contracts/` | Agent contract model (`aieos/v1`) and agent I/O schemas |
| `src/aieos/gateway/` | Tool catalog and runtime permission policy |
| `src/aieos/agents/` | Contract registry (loads and cross-validates definitions) |
| `engineering/agent-definitions/` | Versioned agent contracts (YAML) |
| `engineering/prompts/` | Versioned agent prompts, referenced by contracts |
| `engineering/tool-catalog.yaml` | Every tool and its permission level |
| `engineering/model-routing.yaml` | Providers, model ids per profile, prices, circuit-breaker settings |
| `src/aieos/artifacts/` | S3 artifact store (content-addressed, integrity-checked, presigned URLs) |
| `src/aieos/providers/` | ModelRouter, metered model wrapper, circuit breaker, budget enforcement |
| `src/aieos/sandbox/` | Disposable Docker sandboxes for agent-generated code (no network, read-only root, limits) |
| `migrations/sql/` | Reviewed SQL migrations; `migrations/versions/` wraps them for Alembic |
| `infrastructure/` | Docker, AWS and GitHub Actions assets |
| `docs/` | Blueprint and architecture decision records |

## Quick start (local)

Prerequisites: Docker with Compose v2, Python 3.12+, [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env          # local-only values; never commit .env
make up                       # PostgreSQL, Temporal dev server, S3-compatible store, Jaeger
make install                  # uv sync (runtime + dev)
make migrate                  # apply migrations as the owner role
make test                     # unit tests
make test-integration         # schema tests against the compose PostgreSQL
make check                    # ruff, mypy, import-linter
```

Run the API locally and call it:

```bash
make dev-keys                                         # LOCAL ONLY signing key -> .dev-keys/
make create-org SLUG=acme NAME="Acme" SUB=dev-owner EMAIL=dev@acme.test   # prints the org id
make run-api                                          # or: make up-api (container)
TOKEN=$(make -s token SUB=dev-owner EMAIL=dev@acme.test)
curl -H "Authorization: Bearer $TOKEN" localhost:8000/v1/orgs/<org-id>
```

OpenAPI docs: `http://localhost:8000/docs` (local/test only). Temporal UI `http://localhost:8233`,
Jaeger `http://localhost:16686`.

## API (Increment 1)

| Method | Path | Permission |
| --- | --- | --- |
| GET | `/healthz`, `/readyz` | none |
| GET | `/v1/me` | authenticated |
| GET | `/v1/orgs/{org_id}` | member (non-members get 404) |
| GET, POST | `/v1/orgs/{org_id}/projects` | read: any member; create: admin, owner |
| GET, PATCH, DELETE | `/v1/orgs/{org_id}/projects/{project_id}` | PATCH needs the current `version` (409 if stale) |
| GET, POST | `/v1/orgs/{org_id}/projects/{project_id}/repositories` | register: admin, owner |
| POST | `/v1/orgs/{org_id}/projects/{project_id}/workflows` | member, admin, owner; idempotent by `idempotency_key` |
| GET | `/v1/me/orgs` | authenticated; only the caller's own memberships |
| GET | `/v1/orgs/{org_id}/workflows`, `/v1/orgs/{org_id}/workflows/{id}` | any member |
| GET | `/v1/orgs/{org_id}/workflows/{id}/timeline` | any member; steps, tasks, gates, agent runs with cost |
| GET | `/v1/orgs/{org_id}/costs?days=30` | any member; model spend by day, agent and model |
| POST | `/v1/orgs/{org_id}/workflows/{id}/cancel`, `/clarifications` | creator, admin, owner |
| GET | `/v1/orgs/{org_id}/approvals`, `/v1/orgs/{org_id}/approvals/{id}` | any member |
| POST | `/v1/orgs/{org_id}/approvals/{id}/decision` | approver, owner (and in the project's `approver_roles`); four-eyes for Level 4 |

User CLI: `AIEOS_API_URL`, `AIEOS_TOKEN`, `AIEOS_ORG` in the environment, then
`aieos approvals list`, `aieos approvals approve <id>`, `aieos workflows show <id>`.

### Web dashboard

`/ui/` serves a small static page (no build step, no third-party code) to follow workflows, approve or
reject pending steps and review costs. Sign-in is OIDC authorization code with PKCE in the browser:
register a **public** client at your identity provider with redirect URI `https://<api-host>/ui/` and set
`OIDC_UI_CLIENT_ID` (optionally `OIDC_UI_SCOPES`, `OIDC_UI_AUDIENCE`, and `OIDC_UI_EXTRA_ORIGINS` when the
provider's token endpoint is on another origin than the issuer). The access token is kept in
`sessionStorage` only. The page is served with a strict Content-Security-Policy (scripts and styles from
this origin only; network calls only to the API and the identity provider; no framing). In `local`/`test`,
or when no UI client is configured, the sign-in page also accepts a pasted token
(`aieos-admin dev-token`).

Organizations and memberships are provisioned only with `aieos-admin` (owner role), never through the API.

## Security rules for contributors

- Never commit credentials. Configuration comes from environment variables; secrets from AWS Secrets Manager.
- Agents call tools only through the tool gateway; never import `aieos.integrations` from agent or orchestration code.
- Every schema change ships as a new migration. Never edit an applied migration.
- No cloud resources, GitHub repositories or paid services are created by any script in this repository
  without an explicit, documented human action.
