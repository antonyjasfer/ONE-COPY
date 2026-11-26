-- Idempotent PostgreSQL schema for "One Copy — Did My Submission Go Through?"

CREATE TABLE IF NOT EXISTS trials (
    id UUID PRIMARY KEY,
    visitor_id UUID NOT NULL,
    experiment TEXT NOT NULL,
    cohort TEXT NOT NULL,
    variant TEXT NULL,
    failure_mode TEXT NULL,
    state TEXT NOT NULL,
    title VARCHAR(80) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exposed_at TIMESTAMPTZ NULL,
    deadline_at TIMESTAMPTZ NULL,
    checked_empty_at TIMESTAMPTZ NULL,
    displayed_at TIMESTAMPTZ NULL,
    closed_at TIMESTAMPTZ NULL,
    final_entry_count SMALLINT NULL,
    CONSTRAINT uq_trials_experiment_cohort_visitor UNIQUE (experiment, cohort, visitor_id)
);

CREATE INDEX IF NOT EXISTS idx_trials_visitor_experiment_state
    ON trials (visitor_id, experiment, state);

CREATE INDEX IF NOT EXISTS idx_trials_experiment_cohort_variant
    ON trials (experiment, cohort, variant, failure_mode);

CREATE TABLE IF NOT EXISTS entries (
    id UUID PRIMARY KEY,
    trial_id UUID NOT NULL REFERENCES trials(id) ON DELETE CASCADE,
    action_id UUID NOT NULL,
    receipt_code VARCHAR(8) NOT NULL UNIQUE,
    saved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_entries_trial_action UNIQUE (trial_id, action_id)
);

CREATE INDEX IF NOT EXISTS idx_entries_trial_saved
    ON entries (trial_id, saved_at DESC);

CREATE TABLE IF NOT EXISTS actions (
    trial_id UUID NOT NULL REFERENCES trials(id) ON DELETE CASCADE,
    action_id UUID NOT NULL,
    intent TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (trial_id, action_id)
);

CREATE TABLE IF NOT EXISTS allocation_counters (
    experiment TEXT NOT NULL,
    cohort TEXT NOT NULL,
    variant TEXT NOT NULL,
    next_index BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (experiment, cohort, variant)
);

CREATE TABLE IF NOT EXISTS event_ledger (
    trial_id UUID NOT NULL REFERENCES trials(id) ON DELETE CASCADE,
    event_name TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatch_state TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NULL,
    PRIMARY KEY (trial_id, event_name)
);
