"""Migration 0001: the first tables, the runtime role and the append-only audit log.

Never edit this file once merged: the runner stores its checksum and refuses to
start if a past migration changed. Fix forward with a new migration instead.
"""

NAME = "initial"

SQL = r"""
-- The runtime role. Login users for the app are created by deploy as members of it.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'agentzaaheen_app') THEN
        CREATE ROLE agentzaaheen_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    -- Roles are shared by the whole server: refuse one that already exists with unsafe powers.
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'agentzaaheen_app'
            AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolreplication OR rolcanlogin)
    ) THEN
        RAISE EXCEPTION 'role agentzaaheen_app exists with unsafe attributes'
            USING ERRCODE = 'invalid_role_specification';
    END IF;
END
$$;

REVOKE ALL ON SCHEMA agentzaaheen FROM PUBLIC;
GRANT USAGE ON SCHEMA agentzaaheen TO agentzaaheen_app;

-- ------------------------------------------------------------------ records (CONTRACTS §1-4)
CREATE TABLE agentzaaheen.events (
    id              uuid PRIMARY KEY,
    tenant_id       uuid NOT NULL,
    project_id      uuid NOT NULL,
    type            text NOT NULL,
    source          text NOT NULL,
    vendor_event_id text,
    idempotency_key text NOT NULL,
    occurred_at     timestamptz NOT NULL,
    received_at     timestamptz NOT NULL,
    schema_version  integer NOT NULL CHECK (schema_version >= 1),
    payload         jsonb NOT NULL,
    trust           text NOT NULL CHECK (trust IN ('trusted', 'untrusted')),
    data_class      text NOT NULL CHECK (data_class IN ('public', 'internal', 'customer-pii')),
    subject_ref     text,
    UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX events_by_project ON agentzaaheen.events (tenant_id, project_id, received_at DESC);

CREATE TABLE agentzaaheen.runs (
    id                uuid PRIMARY KEY,
    tenant_id         uuid NOT NULL,
    project_id        uuid NOT NULL,
    agent             text NOT NULL,
    agent_version     text NOT NULL,
    trigger_event_ids uuid[] NOT NULL,
    prompt_hash       text NOT NULL CHECK (prompt_hash ~ '^[0-9a-f]{64}$'),
    model             jsonb,
    data_class_max    text NOT NULL CHECK (data_class_max IN ('public', 'internal', 'customer-pii')),
    trust             text NOT NULL CHECK (trust IN ('trusted', 'untrusted')),
    steps             integer NOT NULL CHECK (steps >= 0),
    tokens_in         bigint NOT NULL CHECK (tokens_in >= 0),
    tokens_out        bigint NOT NULL CHECK (tokens_out >= 0),
    cost_minor_units  bigint NOT NULL CHECK (cost_minor_units >= 0),
    started_at        timestamptz NOT NULL,
    duration_ms       bigint NOT NULL CHECK (duration_ms >= 0),
    outcome           text NOT NULL
        CHECK (outcome IN ('ok', 'nothing_found', 'could_not_run', 'budget_exceeded', 'denied')),
    error_code        text,
    CHECK (outcome IN ('ok', 'nothing_found') OR error_code IS NOT NULL)
);
CREATE INDEX runs_by_project ON agentzaaheen.runs (tenant_id, project_id, started_at DESC);

CREATE TABLE agentzaaheen.findings (
    id          uuid PRIMARY KEY,
    tenant_id   uuid NOT NULL,
    project_id  uuid NOT NULL,
    run_id      uuid REFERENCES agentzaaheen.runs (id),
    kind        text NOT NULL CHECK (kind IN ('incident', 'risk', 'opportunity', 'info', 'question')),
    severity    text NOT NULL CHECK (severity IN ('P1', 'P2', 'P3')),
    title       text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
    body        text NOT NULL,
    evidence    jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'array' AND jsonb_array_length(evidence) >= 1),
    metric_refs jsonb NOT NULL,
    trust       text NOT NULL CHECK (trust IN ('trusted', 'untrusted')),
    data_class  text NOT NULL CHECK (data_class IN ('public', 'internal', 'customer-pii')),
    status      text NOT NULL CHECK (status IN ('open', 'acknowledged', 'resolved', 'dismissed')),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX findings_by_project ON agentzaaheen.findings (tenant_id, project_id, created_at DESC);

CREATE TABLE agentzaaheen.actions (
    id                uuid PRIMARY KEY,
    tenant_id         uuid NOT NULL,
    project_id        uuid NOT NULL,
    run_id            uuid NOT NULL REFERENCES agentzaaheen.runs (id),
    tool              text NOT NULL,
    level             text NOT NULL CHECK (level IN ('draft', 'act')),
    payload           jsonb NOT NULL,
    target_kind       text NOT NULL,
    target_ref        text NOT NULL,
    data_classes      text[] NOT NULL
        CHECK (data_classes <@ ARRAY['public', 'internal', 'customer-pii']::text[]),
    leak_check_passed boolean NOT NULL,
    idempotency_key   text NOT NULL,
    status            text NOT NULL
        CHECK (status IN ('proposed', 'approved', 'rejected', 'sent', 'failed', 'expired')),
    approved_by       uuid,
    approved_at       timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key),
    -- The same rules as the SDK model, enforced again by the database.
    CHECK (status NOT IN ('approved', 'sent', 'failed')
        OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
    CHECK (status NOT IN ('approved', 'sent', 'failed') OR leak_check_passed),
    CHECK (status NOT IN ('proposed', 'rejected') OR (approved_by IS NULL AND approved_at IS NULL))
);
CREATE INDEX actions_by_project ON agentzaaheen.actions (tenant_id, project_id, created_at DESC);

-- ------------------------------------------------------------------ sealed credentials (CR-1)
-- Ciphertext only: there is deliberately no column that could hold a plaintext secret.
CREATE TABLE agentzaaheen.sealed_secrets (
    tenant_id      uuid NOT NULL,
    project_id     uuid NOT NULL,
    connector      text NOT NULL CHECK (connector ~ '^[a-z][a-z0-9_-]{0,63}$'),
    name           text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{0,63}$'),
    environment    text NOT NULL CHECK (environment IN ('production', 'staging')),
    format_version smallint NOT NULL CHECK (format_version = 1),
    master_key_id  text NOT NULL CHECK (char_length(master_key_id) BETWEEN 1 AND 64),
    wrapped_dek    bytea NOT NULL CHECK (octet_length(wrapped_dek) >= 60),
    ciphertext     bytea NOT NULL CHECK (octet_length(ciphertext) >= 29),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id, connector, name, environment)
);

-- ------------------------------------------------------------------ kill switches (SECURITY §6.8)
CREATE TABLE agentzaaheen.kill_switches (
    tenant_id  uuid NOT NULL,
    scope      text NOT NULL CHECK (scope IN ('all', 'project', 'agent')),
    ref        text NOT NULL,
    engaged    boolean NOT NULL,
    changed_by text NOT NULL CHECK (changed_by ~ '^(user|system):[A-Za-z0-9_.-]{1,64}$'),
    changed_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, scope, ref),
    CHECK ((scope = 'all') = (ref = ''))
);

-- ------------------------------------------------------------------ audit log (SP-9)
-- One hash chain per tenant. Rows are only ever added; see the triggers below.
CREATE TABLE agentzaaheen.audit_log (
    tenant_id      uuid NOT NULL,
    seq            bigint NOT NULL CHECK (seq >= 1),
    at             timestamptz NOT NULL,
    actor          text NOT NULL,
    action         text NOT NULL,
    outcome        text NOT NULL CHECK (outcome IN ('allow', 'deny', 'info')),
    reason_code    text NOT NULL,
    correlation_id uuid NOT NULL,
    project_id     uuid,
    target_ref     text,
    details        jsonb NOT NULL CHECK (jsonb_typeof(details) = 'object'),
    prev_hash      text NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    hash           text NOT NULL CHECK (hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, seq)
);

-- A new row must link to the row before it, so the chain cannot fork or skip.
CREATE FUNCTION agentzaaheen.audit_log_link_check() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    previous text;
BEGIN
    IF NEW.seq = 1 THEN
        IF NEW.prev_hash <> repeat('0', 64) THEN
            RAISE EXCEPTION 'first audit row must link to the genesis hash' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    SELECT a.hash INTO previous FROM agentzaaheen.audit_log a
        WHERE a.tenant_id = NEW.tenant_id AND a.seq = NEW.seq - 1;
    IF previous IS NULL OR previous <> NEW.prev_hash THEN
        RAISE EXCEPTION 'audit row does not link to the previous row' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION agentzaaheen.audit_log_is_append_only() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'the audit log is append-only' USING ERRCODE = 'insufficient_privilege';
END
$$;

CREATE TRIGGER audit_log_link BEFORE INSERT ON agentzaaheen.audit_log
    FOR EACH ROW EXECUTE FUNCTION agentzaaheen.audit_log_link_check();
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON agentzaaheen.audit_log
    FOR EACH ROW EXECUTE FUNCTION agentzaaheen.audit_log_is_append_only();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON agentzaaheen.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION agentzaaheen.audit_log_is_append_only();

-- ------------------------------------------------------------------ least privilege for the app
REVOKE ALL ON ALL TABLES IN SCHEMA agentzaaheen FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA agentzaaheen FROM PUBLIC;
GRANT SELECT ON agentzaaheen.schema_migrations TO agentzaaheen_app;
GRANT SELECT, INSERT ON agentzaaheen.events, agentzaaheen.runs, agentzaaheen.findings, agentzaaheen.actions TO agentzaaheen_app;
GRANT SELECT, INSERT ON agentzaaheen.audit_log TO agentzaaheen_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON agentzaaheen.sealed_secrets TO agentzaaheen_app;
GRANT SELECT, INSERT, UPDATE ON agentzaaheen.kill_switches TO agentzaaheen_app;
"""
