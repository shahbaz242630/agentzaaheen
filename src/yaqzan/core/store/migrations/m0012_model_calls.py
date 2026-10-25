"""Migration 0012: what each Claude call cost, so the monthly caps survive a restart (F10 slice 2b, ADR-033).

``model_calls`` holds one row per Claude call that was sent: when, what it was
for (its ``use``, which has its own monthly cap), which model, and what it
really cost in millionths of a USD. The caps are read by adding up a month's
rows, so a restart, a crash or a second process cannot reset them. The rows
hold no prompt and no answer, only the cost.

It is tenant-level (the Board's spend, not one workspace's), behind the tenant
wall, and append-only: the app may add and read rows, never change or delete
one, so a spent amount can only go up.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "model_calls"

SQL = r"""
CREATE TABLE yaqzan.model_calls (
    tenant_id  uuid NOT NULL,
    call_id    uuid NOT NULL,
    at         timestamptz NOT NULL,
    use        text NOT NULL CHECK (use ~ '^[a-z][a-z-]{0,31}$'),
    model      text NOT NULL CHECK (model ~ '^[a-z0-9][a-z0-9.-]{0,63}$'),
    cost_micro bigint NOT NULL CHECK (cost_micro >= 0),
    PRIMARY KEY (tenant_id, call_id)
);

CREATE INDEX model_calls_by_month ON yaqzan.model_calls (tenant_id, use, at);

ALTER TABLE yaqzan.model_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE yaqzan.model_calls FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_wall ON yaqzan.model_calls
    USING (tenant_id = (SELECT yaqzan.current_tenant()))
    WITH CHECK (tenant_id = (SELECT yaqzan.current_tenant()));

REVOKE ALL ON yaqzan.model_calls FROM PUBLIC;
GRANT SELECT, INSERT ON yaqzan.model_calls TO yaqzan_app;
"""
