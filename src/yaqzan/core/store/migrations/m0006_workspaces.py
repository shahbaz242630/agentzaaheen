"""Migration 0006: tenants, workspaces and the database walls between them (F17 slice 1, ADR-039).

A tenant is a customer company (us now, a white-label buyer later); a
workspace is one project inside it. Today's ``project_id`` column is the
workspace id. Every row of a project-level table must belong to a workspace
that exists.

The walls are Postgres row-level security, enabled and forced on every table,
so even the table owner is held by them. Each transaction names its tenant
(and, for project-level tables, its workspace) with ``set_config(..., true)``;
a transaction that names none is refused, it never sees everything.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "workspaces"

SQL = r"""
CREATE TABLE yaqzan.tenants (
    tenant_id  uuid PRIMARY KEY,
    name       text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Stores and files are named by workspace_id, never by its name or slug (ADR-039, AnythingLLM #6658).
CREATE TABLE yaqzan.workspaces (
    tenant_id    uuid NOT NULL REFERENCES yaqzan.tenants (tenant_id),
    workspace_id uuid NOT NULL,
    slug         text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND char_length(slug) <= 64),
    name         text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, workspace_id),
    UNIQUE (tenant_id, slug)
);

-- Rows saved before this migration keep working: their tenants and workspaces are created here.
INSERT INTO yaqzan.tenants (tenant_id, name)
SELECT DISTINCT tenant_id, 'Tenant' FROM (
    SELECT tenant_id FROM yaqzan.events UNION SELECT tenant_id FROM yaqzan.runs
    UNION SELECT tenant_id FROM yaqzan.findings UNION SELECT tenant_id FROM yaqzan.actions
    UNION SELECT tenant_id FROM yaqzan.sealed_secrets UNION SELECT tenant_id FROM yaqzan.revenue_subscriptions
    UNION SELECT tenant_id FROM yaqzan.revenue_payments UNION SELECT tenant_id FROM yaqzan.revenue_refunds
    UNION SELECT tenant_id FROM yaqzan.source_syncs
) seen;
INSERT INTO yaqzan.workspaces (tenant_id, workspace_id, slug, name)
SELECT tenant_id, project_id, 'w-' || replace(project_id::text, '-', ''), 'Unnamed workspace' FROM (
    SELECT tenant_id, project_id FROM yaqzan.events UNION SELECT tenant_id, project_id FROM yaqzan.runs
    UNION SELECT tenant_id, project_id FROM yaqzan.findings
    UNION SELECT tenant_id, project_id FROM yaqzan.actions
    UNION SELECT tenant_id, project_id FROM yaqzan.sealed_secrets
    UNION SELECT tenant_id, project_id FROM yaqzan.revenue_subscriptions
    UNION SELECT tenant_id, project_id FROM yaqzan.revenue_payments
    UNION SELECT tenant_id, project_id FROM yaqzan.revenue_refunds
    UNION SELECT tenant_id, project_id FROM yaqzan.source_syncs
) seen;

-- The tenant and workspace this transaction works for. Unset means refused (fail closed).
CREATE FUNCTION yaqzan.current_tenant() RETURNS uuid
    LANGUAGE plpgsql STABLE SET search_path = pg_catalog AS $$
DECLARE
    value text := current_setting('app.tenant_id', true);
BEGIN
    IF value IS NULL OR value = '' THEN
        RAISE EXCEPTION 'no tenant is set for this transaction' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN value::uuid;
END
$$;

CREATE FUNCTION yaqzan.current_workspace() RETURNS uuid
    LANGUAGE plpgsql STABLE SET search_path = pg_catalog AS $$
DECLARE
    value text := current_setting('app.workspace_id', true);
BEGIN
    IF value IS NULL OR value = '' THEN
        RAISE EXCEPTION 'no workspace is set for this transaction' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN value::uuid;
END
$$;

-- Project-level tables: one tenant and one workspace. Each row must name a workspace that exists.
-- ``(SELECT ...)`` makes Postgres work the function out once per query, not once per row.
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['events', 'runs', 'findings', 'actions', 'sealed_secrets',
        'revenue_subscriptions', 'revenue_payments', 'revenue_refunds', 'source_syncs']
    LOOP
        EXECUTE format('ALTER TABLE yaqzan.%I ADD CONSTRAINT %I FOREIGN KEY (tenant_id, project_id)'
            ' REFERENCES yaqzan.workspaces (tenant_id, workspace_id)', t, t || '_workspace_fk');
        EXECUTE format('ALTER TABLE yaqzan.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE yaqzan.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY workspace_wall ON yaqzan.%I'
            ' USING (tenant_id = (SELECT yaqzan.current_tenant())'
            ' AND project_id = (SELECT yaqzan.current_workspace()))'
            ' WITH CHECK (tenant_id = (SELECT yaqzan.current_tenant())'
            ' AND project_id = (SELECT yaqzan.current_workspace()))', t);
    END LOOP;
    -- Tenant-level tables: the whole tenant, never another one.
    FOREACH t IN ARRAY ARRAY['tenants', 'workspaces', 'audit_log', 'kill_switches', 'registry_requests',
        'registry_switches', 'passkeys']
    LOOP
        EXECUTE format('ALTER TABLE yaqzan.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE yaqzan.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY tenant_wall ON yaqzan.%I'
            ' USING (tenant_id = (SELECT yaqzan.current_tenant()))'
            ' WITH CHECK (tenant_id = (SELECT yaqzan.current_tenant()))', t);
    END LOOP;
END
$$;

REVOKE ALL ON yaqzan.tenants, yaqzan.workspaces FROM PUBLIC;
REVOKE ALL ON FUNCTION yaqzan.current_tenant(), yaqzan.current_workspace() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION yaqzan.current_tenant(), yaqzan.current_workspace() TO yaqzan_app;
GRANT SELECT, INSERT ON yaqzan.tenants, yaqzan.workspaces TO yaqzan_app;
GRANT UPDATE (name) ON yaqzan.tenants, yaqzan.workspaces TO yaqzan_app;
"""
