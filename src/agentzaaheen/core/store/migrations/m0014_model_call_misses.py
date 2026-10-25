"""Migration 0014: each Claude run that did not run, and why (F10 slice 3a, ADR-033).

``model_call_misses`` holds one row per cloud ask that ended without an answer:
refused before sending (a spend cap reached, customer data found, a level the
role may not use), Claude unreachable, or Claude declining. The morning briefing
names them, so a run that did not happen is never mistaken for one that did, and
is never quietly answered by a weaker model instead.

A row holds codes only: when, which role, what for (its ``use``), which model was
wanted, the outcome code, a finer reason code if there is one, and the run's id.
No prompt, no answer.

It is tenant-level (the Board's view, like ``model_calls``), behind the tenant
wall, and append-only: the app may add and read rows, never change or delete one.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "model_call_misses"

SQL = r"""
CREATE TABLE agentzaaheen.model_call_misses (
    tenant_id uuid NOT NULL,
    miss_id   uuid NOT NULL,
    at        timestamptz NOT NULL,
    role      text NOT NULL CHECK (role ~ '^[a-z][a-z0-9-]{0,63}$'),
    use       text NOT NULL CHECK (use ~ '^[a-z][a-z-]{0,31}$'),
    model     text NOT NULL CHECK (model ~ '^[a-z0-9][a-z0-9.-]{0,63}$'),
    outcome   text NOT NULL CHECK (outcome ~ '^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$' AND char_length(outcome) <= 80),
    detail    text CHECK (detail ~ '^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$' AND char_length(detail) <= 80),
    run_id    text NOT NULL CHECK (run_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$'),
    PRIMARY KEY (tenant_id, miss_id)
);

CREATE INDEX model_call_misses_by_time ON agentzaaheen.model_call_misses (tenant_id, at);

ALTER TABLE agentzaaheen.model_call_misses ENABLE ROW LEVEL SECURITY;
ALTER TABLE agentzaaheen.model_call_misses FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_wall ON agentzaaheen.model_call_misses
    USING (tenant_id = (SELECT agentzaaheen.current_tenant()))
    WITH CHECK (tenant_id = (SELECT agentzaaheen.current_tenant()));

REVOKE ALL ON agentzaaheen.model_call_misses FROM PUBLIC;
GRANT SELECT, INSERT ON agentzaaheen.model_call_misses TO agentzaaheen_app;
"""
