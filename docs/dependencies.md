# Dependency register

Every runtime dependency needs a line here before it is added (AGENTS.md rule 5).

| Package | Since | Why it is needed | Alternative rejected |
| --- | --- | --- | --- |
| pydantic | M1 | Contracts, I/O schemas, settings; native to FastAPI | dataclasses + manual validation |
| pyyaml | M1 | Human-reviewable agent definitions and tool catalog (`safe_load` only) | JSON (no comments for reviewers) |
| alembic | M1 | Ordered, recorded migrations; runs reviewed SQL verbatim | hand-rolled migration table |
| sqlalchemy[asyncio] | M1 | Required by Alembic; async engine and pooling for the API (asyncio extra pulls greenlet) | raw psycopg pool (no Alembic integration) |
| psycopg[binary] | M1 | PostgreSQL driver (sync + async) | asyncpg (no sync API for Alembic) |
| fastapi | I1 | HTTP API with Pydantic-native validation and OpenAPI | Litestar (smaller ecosystem) |
| uvicorn | I1 | ASGI server | hypercorn |
| pydantic-settings | I1 | Typed, fail-fast environment configuration | os.environ parsing by hand |
| pyjwt[crypto] | I1 | JWT verification and cached JWKS client; `crypto` adds RSA/EC | authlib (larger surface than needed) |
| structlog | I1 | JSON logs with a redaction processor and context binding | stdlib logging + custom formatter |
| opentelemetry-sdk | I1 | Tracing per request; trace id in logs | none (required by the observability decision) |
| opentelemetry-exporter-otlp-proto-http | I1 | OTLP export to the ADOT collector / Jaeger | gRPC exporter (heavier dependency tree) |
| httpx (dev) | I1 | FastAPI TestClient | — |
| boto3 | I2 | S3 artifact store (checksums, SSE-KMS, presigned POST policies) | aioboto3 (extra layer over botocore; thread offload is enough at our call rates) |
| ~~openai-agents~~ | I2 → removed B14b | Replaced by our own loop and HTTP adapters (ADR-0002): the platform must not depend on any model vendor | — |
| httpx | B14b (runtime; dev since I1) | Model provider adapters over plain HTTPS; async, timeouts, mock transport for tests | vendor SDKs (one per vendor, ties the code to vendors' types) |
| moto[s3] (dev) | I2 | In-process S3 for artifact tests, no network or Docker needed | LocalStack (container) |
| boto3-stubs[s3] (dev) | I2 | Type checking for the S3 client | — |
| temporalio | I3 | Durable workflows (decided); Pydantic data converter and the workflow sandbox | — |
| pytest-asyncio (dev) | I3 | Async tests against Temporal's test server | anyio plugin |

`pip-audit` reported no known vulnerabilities on 2026-11-21 (after Increment 3). Starlette now prefers the `httpx2` package for
TestClient (it emits a deprecation warning with `httpx`); switching is a dev-only change, pending a review of that package.

No vendor SDK is planned: model access stays in `aieos.providers.adapters` (ADR-0002).
The user CLI (`aieos`) uses only the standard library, so typer is no longer planned.
| aws-cdk-lib, constructs | AWS IaC (infra group only) | Infrastructure as code in the platform's language, testable offline (ADR-0003); never installed in the platform images | Terraform (separate language and toolchain) |
