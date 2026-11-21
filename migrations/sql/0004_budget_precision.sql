-- Workflow totals were numeric(12,4) while per-call costs are numeric(12,6): every metered call
-- rounded the running total, so recorded spend drifted from the sum of calls (found by
-- tests/integration/test_agent_runner.py). Totals now use the same precision as costs.
-- Widening numeric scale is safe; on large tables run it in a maintenance window (table rewrite).
ALTER TABLE workflows
    ALTER COLUMN budget_usd TYPE numeric(14, 6),
    ALTER COLUMN spent_usd TYPE numeric(14, 6);
