-- B13: every sandbox and every push of a workflow uses the same base commit of the repository.
-- Set once, on first use, by the tool gateway (first writer wins); never changed afterwards.
ALTER TABLE workflows
    ADD COLUMN base_commit_sha text CHECK (base_commit_sha ~ '^[0-9a-f]{40}$');

CREATE FUNCTION workflows_base_commit_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.base_commit_sha IS NOT NULL AND NEW.base_commit_sha IS DISTINCT FROM OLD.base_commit_sha THEN
        RAISE EXCEPTION 'workflows.base_commit_sha cannot change once set';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER workflows_base_commit_immutable
    BEFORE UPDATE OF base_commit_sha ON workflows
    FOR EACH ROW EXECUTE FUNCTION workflows_base_commit_immutable();
