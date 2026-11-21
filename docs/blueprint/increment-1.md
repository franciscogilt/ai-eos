# Increment 1 — Tenant-safe API skeleton (B5, B6, B7)

## Decisions
- **Org id in the path, membership checked under RLS.** Each request opens one transaction, sets
  `app.org_id` transaction-locally, then reads the caller's membership. Non-members see 404, members
  lacking permission see 403. No SECURITY DEFINER lookup is needed. Listing "my organizations" across
  tenants needs one and is deferred to the dashboard increment.
- **Explicit SQL, no ORM.** RLS behaviour stays visible in review. Repositories take a connection;
  routes own transaction boundaries.
- **Just-in-time users.** A valid token creates the `users` row on first use. Access still requires a
  membership granted by `aieos-admin`.
- **Asymmetric JWTs only.** `none` and HS* are rejected by configuration validation and again per token.
- **Startup refuses unsafe DB roles.** Superuser, BYPASSRLS, table owner, or not in `aieos_app` stops the process.
- **Dev auth uses a local JWKS file** (`aieos-admin dev-keys` / `dev-token`), allowed only when
  `ENVIRONMENT` is `local` or `test`. Production uses `OIDC_JWKS_URL` over https.

## Verified (2026-11-21, PostgreSQL 16)
- 134 tests pass: 96 unit, 38 integration (API + schema). The API tests use a one-connection pool to
  prove tenant settings never leak between requests.
- ruff, ruff format, mypy --strict (40 files), import-linter: clean. pip-audit: no known vulnerabilities.
- Real server smoke test: readiness, 401 without a token, org read and project create with a dev token;
  the token never appeared in logs.
- Not run: `docker build` / `make up-api` (no Docker daemon available).

## Risks carried forward
- `/v1/orgs/{org_id}` resolves membership with two queries per request; cache in B10 if it shows in p95.
- Offset pagination; switch to keyset pagination before lists exceed a few thousand rows.
