# Increment 2 — Artifact store and model router (B8, B9)

## B8 Artifact store (`src/aieos/artifacts/store.py`)
- Key: `orgs/<org>/workflows/<workflow|_unscoped>/<kind>/<sha256>`. Same bytes in the same scope are
  stored once; a repeated `put` returns the same row. Scoping by org avoids cross-tenant de-duplication,
  which would reveal whether another tenant holds a file.
- S3 verifies SHA-256 on upload (`ChecksumSHA256`); every read re-verifies size and hash and raises
  `ArtifactIntegrityError` on mismatch.
- Objects are tagged `retention` and `classification` so S3 lifecycle rules can apply the retention policy.
- Downloads: presigned GET, lifetime clamped to `PRESIGN_TTL_SECONDS` (max 900). Metadata lookup runs under
  RLS, so another tenant cannot presign your artifact.
- Sandbox uploads: presigned POST for one fixed key with a `content-length-range` condition. `finalize_upload`
  accepts only keys under the caller's own org/workflow `uploads/` prefix, moves the object to its content
  address, and deletes the upload.

## B9 Model routing (`src/aieos/providers/`)
- Contracts choose the **provider**; `engineering/model-routing.yaml` chooses the **model id** per provider and
  profile. Separation of duties therefore stays in reviewed contracts.
  `check_model_independence` refuses to start if QA/Security shares any primary or fallback model with the Developer.
- Every model the runtime receives is a `MeteredModel`:
  1. Budget check before the call: workflow USD and tokens, plus the agent run's contract limits.
  2. Candidates in order (primary, then fallbacks); a provider with an open circuit is skipped.
  3. Only transient errors (connection, timeout, rate limit, 5xx) fall through; bad requests raise.
  4. Usage and cost are recorded after the call, idempotent per provider request id. Audit events fire when
     80% and 100% of a workflow budget are crossed.
- Unpriced or placeholder models are refused. Streaming is disabled, because metering needs the final usage.
- `RunModelProvider` pins each run to its contract's model, so an agent cannot switch models by name.
- Prompt-cache writes are metered separately (migration 0002) and priced at
  `cache_write_input_per_mtok`, or at the input rate when that is unset.
- `aieos-admin publish-agents` writes contracts as immutable `agent_versions`. It refuses changed content under an
  existing version number.

## Verified (2026-11-21, PostgreSQL 16, moto S3)
- 167 tests pass: 117 unit, 50 integration. These include a real OpenAI Agents SDK `Runner.run` through the
  metered model, fallback, circuit-breaker behaviour, budget warning → exhaustion → refusal, a replayed request
  that is not double-charged, cross-tenant artifact access, tampering detection, and upload-key confinement.
- Alembic upgrades a fresh database through 0002. ruff, mypy --strict (48 files), import-linter and pip-audit are clean.
- Not run: calls to a real model provider (no keys, and no model ids or prices configured), or a real S3/VersityGW endpoint.

## Open items
- Fill `models` and `pricing` in `engineering/model-routing.yaml` (placeholders are refused at runtime).
- The second-provider slot is named `anthropic` to match the current QA contract. Point it at any OpenAI-compatible
  endpoint, or rename it in a new contract version.
- `put` holds the whole artifact in memory (default limit 100 MiB). Switch to multipart streaming if patches or logs
  approach that.
