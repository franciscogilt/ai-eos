-- Increment 2: providers report prompt-cache writes separately (and may bill them at a
-- different rate), so metering stores them. Additive with a default: safe on a live table.
ALTER TABLE model_usage
    ADD COLUMN cache_write_input_tokens integer NOT NULL DEFAULT 0
        CHECK (cache_write_input_tokens >= 0);
