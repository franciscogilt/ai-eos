-- AI-EOS initial schema (Milestone 1)
-- Target: PostgreSQL 16+ (RDS PostgreSQL 17 in AWS).
-- Tenant isolation: shared schema, org_id on every tenant table, row-level security
-- keyed on the session setting `app.org_id`, composite foreign keys (org_id, id)
-- so a row can never reference a parent in another tenant.
--
-- Conventions
--   * Primary keys are UUIDs supplied by the application (UUIDv7, time-ordered).
--     gen_random_uuid() is only a fallback default.
--   * Status columns are text + CHECK (cheaper to evolve than PG enums).
--   * `version` columns implement optimistic concurrency:
--       UPDATE ... SET version = version + 1 WHERE id = $1 AND version = $2
--   * No secrets are ever stored here. Credentials live in AWS Secrets Manager;
--     rows store only references (ARNs / names) where needed.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Roles. The migration owner owns the tables; the application connects as a
-- login role that is a member of aieos_app and is therefore subject to RLS.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aieos_app') THEN
        CREATE ROLE aieos_app NOLOGIN;
    END IF;
END
$$;

-- Current tenant for this transaction. The application sets it with
--   SELECT set_config('app.org_id', '<uuid>', true);   -- true = transaction-local
CREATE OR REPLACE FUNCTION app_current_org() RETURNS uuid
    LANGUAGE sql STABLE
AS $$
    SELECT nullif(current_setting('app.org_id', true), '')::uuid
$$;

-- ---------------------------------------------------------------------------
-- Identity and tenancy
-- ---------------------------------------------------------------------------
CREATE TABLE organizations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name        text NOT NULL,
    status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'deleted')),
    settings    jsonb NOT NULL DEFAULT '{}'::jsonb,
    version     integer NOT NULL DEFAULT 1,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Users are global (one person may belong to several organizations).
CREATE TABLE users (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    idp_subject       text NOT NULL UNIQUE,          -- OIDC `sub` from the identity provider
    email             text NOT NULL,
    display_name      text,
    status            text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_lower_uq ON users (lower(email));

CREATE TABLE memberships (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL REFERENCES organizations (id) ON DELETE CASCADE,
    user_id     uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role        text NOT NULL CHECK (role IN ('owner', 'admin', 'approver', 'member', 'viewer')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, user_id)
);
CREATE INDEX memberships_user_idx ON memberships (user_id);

CREATE TABLE projects (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid NOT NULL REFERENCES organizations (id) ON DELETE CASCADE,
    slug        text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name        text NOT NULL,
    -- Project policy: auto-approved Level 3 tools, budgets, required gates, etc.
    -- Validated by aieos.gateway.policy.ProjectPolicy before it is written.
    policy      jsonb NOT NULL DEFAULT '{}'::jsonb,
    version     integer NOT NULL DEFAULT 1,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, slug),
    UNIQUE (org_id, id)
);

CREATE TABLE repositories (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                   uuid NOT NULL,
    project_id               uuid NOT NULL,
    provider                 text NOT NULL DEFAULT 'github' CHECK (provider IN ('github')),
    external_id              bigint NOT NULL,         -- GitHub repository id (stable across renames)
    owner_login              text NOT NULL,
    name                     text NOT NULL,
    default_branch           text NOT NULL DEFAULT 'main',
    github_installation_id   bigint NOT NULL,         -- GitHub App installation, not a credential
    status                   text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disconnected')),
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, provider, external_id),
    UNIQUE (org_id, id),
    FOREIGN KEY (org_id, project_id) REFERENCES projects (org_id, id) ON DELETE CASCADE
);
CREATE INDEX repositories_project_idx ON repositories (org_id, project_id);

-- ---------------------------------------------------------------------------
-- Agents. org_id NULL = platform-provided agent visible to every tenant.
-- ---------------------------------------------------------------------------
CREATE TABLE agents (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      uuid REFERENCES organizations (id) ON DELETE CASCADE,
    key         text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{1,62}$'),
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (org_id, key)
);

CREATE TABLE agent_versions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id           uuid REFERENCES organizations (id) ON DELETE CASCADE,
    agent_id         uuid NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
    version          text NOT NULL CHECK (version ~ '^\d+\.\d+\.\d+$'),
    contract         jsonb NOT NULL,                  -- full resolved AgentContract
    contract_sha256  text NOT NULL CHECK (contract_sha256 ~ '^[0-9a-f]{64}$'),
    prompt_sha256    text NOT NULL CHECK (prompt_sha256 ~ '^[0-9a-f]{64}$'),
    status           text NOT NULL DEFAULT 'active' CHECK (status IN ('draft', 'active', 'retired')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_id, version)
);
CREATE INDEX agent_versions_agent_idx ON agent_versions (agent_id, status);

-- ---------------------------------------------------------------------------
-- Workflows and planning
-- ---------------------------------------------------------------------------
CREATE TABLE workflows (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                uuid NOT NULL,
    project_id            uuid NOT NULL,
    repository_id         uuid NOT NULL,
    kind                  text NOT NULL CHECK (kind IN ('feature_delivery')),
    title                 text NOT NULL,
    status                text NOT NULL DEFAULT 'pending' CHECK (status IN (
                              'pending', 'running', 'awaiting_input', 'awaiting_approval',
                              'succeeded', 'failed', 'cancelled')),
    temporal_workflow_id  text NOT NULL UNIQUE,
    temporal_run_id       text,
    idempotency_key       text NOT NULL,              -- supplied by the client on create
    created_by            uuid NOT NULL REFERENCES users (id),
    budget_usd            numeric(12, 4) NOT NULL CHECK (budget_usd > 0),
    budget_tokens         bigint NOT NULL CHECK (budget_tokens > 0),
    spent_usd             numeric(12, 4) NOT NULL DEFAULT 0,
    spent_tokens          bigint NOT NULL DEFAULT 0,
    version               integer NOT NULL DEFAULT 1,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    finished_at           timestamptz,
    UNIQUE (org_id, idempotency_key),
    UNIQUE (org_id, id),
    FOREIGN KEY (org_id, project_id) REFERENCES projects (org_id, id),
    FOREIGN KEY (org_id, repository_id) REFERENCES repositories (org_id, id)
);
CREATE INDEX workflows_project_status_idx ON workflows (org_id, project_id, status, created_at DESC);

CREATE TABLE workflow_steps (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       uuid NOT NULL,
    workflow_id  uuid NOT NULL,
    step_key     text NOT NULL,                       -- e.g. requirements, architecture, plan
    sequence     integer NOT NULL,
    attempt      integer NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    status       text NOT NULL DEFAULT 'pending' CHECK (status IN (
                     'pending', 'running', 'awaiting_input', 'awaiting_approval',
                     'succeeded', 'failed', 'skipped', 'cancelled')),
    error        jsonb,
    started_at   timestamptz,
    finished_at  timestamptz,
    UNIQUE (workflow_id, step_key, attempt),
    UNIQUE (org_id, id),
    FOREIGN KEY (org_id, workflow_id) REFERENCES workflows (org_id, id) ON DELETE CASCADE
);

CREATE TABLE tasks (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              uuid NOT NULL,
    workflow_id         uuid NOT NULL,
    key                 text NOT NULL,                -- stable key from the plan, e.g. T3
    title               text NOT NULL,
    description         text NOT NULL,
    assigned_agent_key  text NOT NULL,
    status              text NOT NULL DEFAULT 'pending' CHECK (status IN (
                            'pending', 'ready', 'running', 'in_review', 'succeeded',
                            'failed', 'cancelled')),
    branch_name         text,
    attempt             integer NOT NULL DEFAULT 0,
    version             integer NOT NULL DEFAULT 1,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_id, key),
    UNIQUE (org_id, id),
    FOREIGN KEY (org_id, workflow_id) REFERENCES workflows (org_id, id) ON DELETE CASCADE
);
CREATE INDEX tasks_workflow_status_idx ON tasks (org_id, workflow_id, status);

CREATE TABLE task_dependencies (
    org_id              uuid NOT NULL,
    task_id             uuid NOT NULL,
    depends_on_task_id  uuid NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id <> depends_on_task_id),
    FOREIGN KEY (org_id, task_id) REFERENCES tasks (org_id, id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, depends_on_task_id) REFERENCES tasks (org_id, id) ON DELETE CASCADE
);
-- Cycle detection is done by the planner before insert (graph is validated as a DAG).
CREATE INDEX task_dependencies_reverse_idx ON task_dependencies (depends_on_task_id);

-- ---------------------------------------------------------------------------
-- Artifacts (content in S3, metadata here)
-- ---------------------------------------------------------------------------
CREATE TABLE artifacts (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id             uuid NOT NULL,
    workflow_id        uuid,
    kind               text NOT NULL CHECK (kind IN (
                           'request', 'requirements_spec', 'architecture_design', 'plan',
                           'patch', 'test_report', 'security_report', 'review_report',
                           'execution_log', 'agent_output', 'tool_result', 'other')),
    s3_bucket          text NOT NULL,
    s3_key             text NOT NULL,
    sha256             text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes         bigint NOT NULL CHECK (size_bytes >= 0),
    content_type       text NOT NULL,
    classification     text NOT NULL DEFAULT 'internal' CHECK (classification IN (
                           'public', 'internal', 'confidential')),
    retention_class    text NOT NULL DEFAULT 'standard' CHECK (retention_class IN (
                           'ephemeral', 'standard', 'audit')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (s3_bucket, s3_key),
    UNIQUE (org_id, id),
    FOREIGN KEY (org_id, workflow_id) REFERENCES workflows (org_id, id) ON DELETE SET NULL (workflow_id)
);
CREATE INDEX artifacts_workflow_idx ON artifacts (org_id, workflow_id, kind);

-- ---------------------------------------------------------------------------
-- Execution records
-- ---------------------------------------------------------------------------
CREATE TABLE agent_runs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              uuid NOT NULL,
    workflow_id         uuid NOT NULL,
    workflow_step_id    uuid,
    task_id             uuid,
    agent_version_id    uuid NOT NULL REFERENCES agent_versions (id),
    model_provider      text NOT NULL,
    model_name          text NOT NULL,
    status              text NOT NULL DEFAULT 'running' CHECK (status IN (
                            'running', 'succeeded', 'failed', 'cancelled', 'budget_exceeded')),
    input_artifact_id   uuid,
    output_artifact_id  uuid,
    trace_id            text,                         -- OpenTelemetry trace id
    error               jsonb,
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    UNIQUE (org_id, id),
    FOREIGN KEY (org_id, workflow_id) REFERENCES workflows (org_id, id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, workflow_step_id) REFERENCES workflow_steps (org_id, id) ON DELETE SET NULL (workflow_step_id),
    FOREIGN KEY (org_id, task_id) REFERENCES tasks (org_id, id) ON DELETE SET NULL (task_id),
    FOREIGN KEY (org_id, input_artifact_id) REFERENCES artifacts (org_id, id) ON DELETE SET NULL (input_artifact_id),
    FOREIGN KEY (org_id, output_artifact_id) REFERENCES artifacts (org_id, id) ON DELETE SET NULL (output_artifact_id)
);
CREATE INDEX agent_runs_workflow_idx ON agent_runs (org_id, workflow_id, started_at);

CREATE TABLE approvals (
    id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                     uuid NOT NULL,
    workflow_id                uuid NOT NULL,
    kind                       text NOT NULL CHECK (kind IN (
                                   'tool_execution', 'merge', 'staging_deploy',
                                   'production_deploy', 'budget_increase', 'policy_exception')),
    permission_level           smallint NOT NULL CHECK (permission_level BETWEEN 3 AND 4),
    tool_name                  text,
    request_digest             text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    summary                    text NOT NULL,         -- human-readable, secret-free
    status                     text NOT NULL DEFAULT 'pending' CHECK (status IN (
                                   'pending', 'approved', 'rejected', 'expired', 'cancelled')),
    requested_by_agent_run_id  uuid,
    requested_by_user_id       uuid REFERENCES users (id),
    decided_by_user_id         uuid REFERENCES users (id),
    decided_at                 timestamptz,
    decision_comment           text,
    expires_at                 timestamptz NOT NULL,
    version                    integer NOT NULL DEFAULT 1,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, id),
    FOREIGN KEY (org_id, workflow_id) REFERENCES workflows (org_id, id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, requested_by_agent_run_id) REFERENCES agent_runs (org_id, id) ON DELETE SET NULL (requested_by_agent_run_id),
    -- A decided approval must name its decider and time.
    CHECK ((status IN ('approved', 'rejected')) = (decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL)),
    -- Four-eyes: a human cannot approve their own request.
    CHECK (requested_by_user_id IS NULL OR decided_by_user_id IS NULL
           OR requested_by_user_id <> decided_by_user_id)
);
CREATE INDEX approvals_pending_idx ON approvals (org_id, status, expires_at) WHERE status = 'pending';
CREATE INDEX approvals_workflow_idx ON approvals (org_id, workflow_id);

CREATE TABLE tool_executions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id             uuid NOT NULL,
    workflow_id        uuid NOT NULL,
    agent_run_id       uuid NOT NULL,
    tool_name          text NOT NULL,
    permission_level   smallint NOT NULL CHECK (permission_level BETWEEN 1 AND 4),
    decision           text NOT NULL CHECK (decision IN ('allow', 'deny', 'require_approval')),
    decision_reason    text NOT NULL,
    approval_id        uuid,
    idempotency_key    text NOT NULL,                 -- derived: workflow/run/tool-call id
    request_digest     text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    status             text NOT NULL DEFAULT 'pending' CHECK (status IN (
                           'pending', 'running', 'succeeded', 'failed', 'denied', 'awaiting_approval')),
    result_artifact_id uuid,
    error              jsonb,
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    UNIQUE (org_id, idempotency_key),
    UNIQUE (org_id, id),
    FOREIGN KEY (org_id, workflow_id) REFERENCES workflows (org_id, id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, agent_run_id) REFERENCES agent_runs (org_id, id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, approval_id) REFERENCES approvals (org_id, id) ON DELETE SET NULL (approval_id),
    FOREIGN KEY (org_id, result_artifact_id) REFERENCES artifacts (org_id, id) ON DELETE SET NULL (result_artifact_id)
);
CREATE INDEX tool_executions_run_idx ON tool_executions (org_id, agent_run_id, started_at);

CREATE TABLE decisions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL,
    workflow_id   uuid NOT NULL,
    agent_run_id  uuid,
    kind          text NOT NULL CHECK (kind IN ('architecture', 'plan', 'scope', 'gate_override', 'other')),
    title         text NOT NULL,
    rationale     text NOT NULL,
    status        text NOT NULL DEFAULT 'accepted' CHECK (status IN ('proposed', 'accepted', 'superseded')),
    artifact_id   uuid,
    created_at    timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (org_id, workflow_id) REFERENCES workflows (org_id, id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, agent_run_id) REFERENCES agent_runs (org_id, id) ON DELETE SET NULL (agent_run_id),
    FOREIGN KEY (org_id, artifact_id) REFERENCES artifacts (org_id, id) ON DELETE SET NULL (artifact_id)
);
CREATE INDEX decisions_workflow_idx ON decisions (org_id, workflow_id);

CREATE TABLE quality_gates (
    id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                     uuid NOT NULL,
    workflow_id                uuid NOT NULL,
    task_id                    uuid,
    gate_key                   text NOT NULL,         -- e.g. tests_pass, no_high_findings
    result                     text NOT NULL CHECK (result IN ('passed', 'failed', 'waived')),
    details                    jsonb NOT NULL DEFAULT '{}'::jsonb,
    evaluated_by_agent_run_id  uuid,
    waived_by_approval_id      uuid,
    evaluated_at               timestamptz NOT NULL DEFAULT now(),
    CHECK (result <> 'waived' OR waived_by_approval_id IS NOT NULL),
    FOREIGN KEY (org_id, workflow_id) REFERENCES workflows (org_id, id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, task_id) REFERENCES tasks (org_id, id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, evaluated_by_agent_run_id) REFERENCES agent_runs (org_id, id) ON DELETE SET NULL (evaluated_by_agent_run_id),
    FOREIGN KEY (org_id, waived_by_approval_id) REFERENCES approvals (org_id, id)
);
CREATE INDEX quality_gates_workflow_idx ON quality_gates (org_id, workflow_id, gate_key, evaluated_at DESC);

CREATE TABLE security_findings (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        uuid NOT NULL,
    workflow_id   uuid NOT NULL,
    task_id       uuid,
    agent_run_id  uuid,
    source        text NOT NULL,                      -- semgrep, bandit, pip-audit, gitleaks, agent
    rule_id       text NOT NULL,
    severity      text NOT NULL CHECK (severity IN ('info', 'low', 'medium', 'high', 'critical')),
    title         text NOT NULL,
    file_path     text,
    line          integer CHECK (line IS NULL OR line > 0),
    fingerprint   text NOT NULL,                      -- stable hash for de-duplication
    status        text NOT NULL DEFAULT 'open' CHECK (status IN (
                      'open', 'fixed', 'accepted_risk', 'false_positive')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, workflow_id, fingerprint),
    FOREIGN KEY (org_id, workflow_id) REFERENCES workflows (org_id, id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, task_id) REFERENCES tasks (org_id, id) ON DELETE SET NULL (task_id),
    FOREIGN KEY (org_id, agent_run_id) REFERENCES agent_runs (org_id, id) ON DELETE SET NULL (agent_run_id)
);
CREATE INDEX security_findings_open_idx ON security_findings (org_id, workflow_id, severity) WHERE status = 'open';

CREATE TABLE model_usage (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              uuid NOT NULL,
    workflow_id         uuid NOT NULL,
    agent_run_id        uuid NOT NULL,
    provider            text NOT NULL,
    model               text NOT NULL,
    provider_request_id text,
    input_tokens        integer NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens integer NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    output_tokens       integer NOT NULL CHECK (output_tokens >= 0),
    cost_usd            numeric(12, 6) NOT NULL CHECK (cost_usd >= 0),
    occurred_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (org_id, provider, provider_request_id),
    FOREIGN KEY (org_id, workflow_id) REFERENCES workflows (org_id, id) ON DELETE CASCADE,
    FOREIGN KEY (org_id, agent_run_id) REFERENCES agent_runs (org_id, id) ON DELETE CASCADE
);
CREATE INDEX model_usage_workflow_idx ON model_usage (org_id, workflow_id, occurred_at);
CREATE INDEX model_usage_org_time_idx ON model_usage (org_id, occurred_at);

-- ---------------------------------------------------------------------------
-- Audit: append-only, hash-chained per tenant (org_id NULL = platform chain).
-- ---------------------------------------------------------------------------
CREATE TABLE audit_events (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id       uuid REFERENCES organizations (id),
    chain_seq    bigint NOT NULL,
    occurred_at  timestamptz NOT NULL DEFAULT now(),
    actor_type   text NOT NULL CHECK (actor_type IN ('user', 'agent', 'system')),
    actor_id     text NOT NULL,
    action       text NOT NULL,                       -- e.g. tool.executed, approval.decided
    target_type  text NOT NULL,
    target_id    text NOT NULL,
    workflow_id  uuid,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- secret-free by contract (redacted upstream)
    prev_hash    bytea,
    hash         bytea NOT NULL,
    UNIQUE NULLS NOT DISTINCT (org_id, chain_seq)
);
CREATE INDEX audit_events_org_time_idx ON audit_events (org_id, occurred_at DESC);
CREATE INDEX audit_events_workflow_idx ON audit_events (workflow_id) WHERE workflow_id IS NOT NULL;

-- Assigns chain_seq / prev_hash / hash. The advisory lock serialises writers per
-- chain so two concurrent inserts cannot fork it. SECURITY DEFINER so the lookup of
-- the previous link is not affected by the caller's RLS context.
CREATE OR REPLACE FUNCTION audit_events_chain() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp
AS $$
DECLARE
    last_seq  bigint;
    last_hash bytea;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('audit:' || coalesce(NEW.org_id::text, 'platform'), 0));

    SELECT chain_seq, hash INTO last_seq, last_hash
    FROM audit_events
    WHERE org_id IS NOT DISTINCT FROM NEW.org_id
    ORDER BY chain_seq DESC
    LIMIT 1;

    NEW.chain_seq := coalesce(last_seq, 0) + 1;
    NEW.prev_hash := last_hash;
    NEW.hash := digest(
        concat_ws('|',
            coalesce(encode(last_hash, 'hex'), 'genesis'),
            coalesce(NEW.org_id::text, 'platform'),
            NEW.chain_seq::text,
            to_char(NEW.occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            NEW.actor_type, NEW.actor_id, NEW.action, NEW.target_type, NEW.target_id,
            coalesce(NEW.workflow_id::text, ''),
            NEW.payload::text),
        'sha256');
    RETURN NEW;
END
$$;

CREATE TRIGGER audit_events_chain_trg
    BEFORE INSERT ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_chain();

CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (% rejected)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END
$$;

CREATE TRIGGER audit_events_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_immutable();

CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_events_immutable();

-- ---------------------------------------------------------------------------
-- Row-level security
-- Policies apply to aieos_app (the API and workers). RLS is deliberately NOT
-- FORCEd: the table owner is the migration/provisioning role, which must create
-- organizations and platform agents across tenants. The application never
-- connects as the owner; CI checks the runtime DSN user is not the owner.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'memberships', 'projects', 'repositories', 'workflows', 'workflow_steps',
        'tasks', 'task_dependencies', 'artifacts', 'agent_runs', 'approvals',
        'tool_executions', 'decisions', 'quality_gates', 'security_findings', 'model_usage'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I TO aieos_app '
            'USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org())', t);
    END LOOP;
END
$$;

-- Organizations: a session sees only its own organization row.
ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON organizations TO aieos_app
    USING (id = app_current_org()) WITH CHECK (id = app_current_org());

-- Agents and versions: platform rows (org_id NULL) are readable by everyone;
-- tenants may only write their own.
ALTER TABLE agents ENABLE ROW LEVEL SECURITY;
CREATE POLICY agents_read ON agents FOR SELECT TO aieos_app
    USING (org_id IS NULL OR org_id = app_current_org());
CREATE POLICY agents_write ON agents FOR ALL TO aieos_app
    USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());

ALTER TABLE agent_versions ENABLE ROW LEVEL SECURITY;
CREATE POLICY agent_versions_read ON agent_versions FOR SELECT TO aieos_app
    USING (org_id IS NULL OR org_id = app_current_org());
CREATE POLICY agent_versions_write ON agent_versions FOR ALL TO aieos_app
    USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());

-- Audit: tenants read and append their own chain only.
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY audit_read ON audit_events FOR SELECT TO aieos_app
    USING (org_id = app_current_org());
CREATE POLICY audit_append ON audit_events FOR INSERT TO aieos_app
    WITH CHECK (org_id = app_current_org());

-- ---------------------------------------------------------------------------
-- Grants. users is global and accessed only through membership-scoped queries
-- in the application; it holds no secrets.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO aieos_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aieos_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM aieos_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aieos_app;
