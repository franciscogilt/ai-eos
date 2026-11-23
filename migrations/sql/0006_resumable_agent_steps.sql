-- Resumable agent steps (ADR-0001 follow-up). A retried `run_agent_step` activity reuses the agent run
-- of the same (workflow, step, attempt) and continues from its last checkpoint, so a worker crash does
-- not repeat model calls, and tool calls keep their call ids (the gateway returns recorded results).
ALTER TABLE agent_runs
    ADD COLUMN step_key text CHECK (step_key IS NULL OR length(step_key) <= 200),
    ADD COLUMN step_attempt integer CHECK (step_attempt IS NULL OR step_attempt >= 1),
    ADD CONSTRAINT agent_runs_step_pair CHECK ((step_key IS NULL) = (step_attempt IS NULL));

CREATE UNIQUE INDEX agent_runs_one_per_step_attempt
    ON agent_runs (org_id, workflow_id, step_key, step_attempt) WHERE step_key IS NOT NULL;

CREATE TABLE agent_run_checkpoints (
    org_id        uuid NOT NULL,
    agent_run_id  uuid NOT NULL,
    state         jsonb NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, agent_run_id),
    FOREIGN KEY (org_id, agent_run_id) REFERENCES agent_runs (org_id, id) ON DELETE CASCADE,
    CONSTRAINT agent_run_checkpoints_size CHECK (pg_column_size(state) <= 16777216)
);

ALTER TABLE agent_run_checkpoints ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON agent_run_checkpoints TO aieos_app
    USING (org_id = app_current_org()) WITH CHECK (org_id = app_current_org());
GRANT SELECT, INSERT, UPDATE, DELETE ON agent_run_checkpoints TO aieos_app;
