-- Increment 3: tool gateway results, stale-execution detection, approval de-duplication.
ALTER TABLE tool_executions
    ADD COLUMN result jsonb,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD CONSTRAINT tool_executions_result_size CHECK (result IS NULL OR pg_column_size(result) <= 65536);

-- At most one pending approval per exact request (workflow, tool, argument digest).
CREATE UNIQUE INDEX approvals_one_pending_per_request
    ON approvals (workflow_id, tool_name, request_digest) WHERE status = 'pending';

CREATE INDEX approvals_request_idx ON approvals (org_id, workflow_id, tool_name, request_digest);
CREATE INDEX agent_runs_version_idx ON agent_runs (org_id, workflow_id, agent_version_id);
