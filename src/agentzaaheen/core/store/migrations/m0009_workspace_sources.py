"""Migration 0009: which repository each workspace reads (F17 slice 5a, ADR-039).

A workspace may be connected to one GitHub repository (``kind`` 'github',
``ref`` its ``owner/name``). A repository belongs to at most one workspace of
a tenant, so two projects never share one by mistake. The row is
project-level, behind the same wall as the rest; nothing lands in an archived
workspace; deleting a workspace for good removes it with everything else.
It is a name only: the read-only token that reads the repository stays in the
vault (or the environment on the laptop), never here.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "workspace_sources"

SQL = r"""
CREATE TABLE agentzaaheen.workspace_sources (
    tenant_id  uuid NOT NULL,
    project_id uuid NOT NULL,
    kind       text NOT NULL CHECK (kind IN ('github')),
    ref        text NOT NULL CHECK (ref ~ '^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$'
                                    AND split_part(ref, '/', 2) NOT IN ('.', '..')),
    added_by   text NOT NULL CHECK (added_by ~ '^(user|system):[A-Za-z0-9_.-]{1,64}$'),
    added_at   timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, project_id, kind),
    FOREIGN KEY (tenant_id, project_id) REFERENCES agentzaaheen.workspaces (tenant_id, workspace_id)
);
-- GitHub names are not case-sensitive: Owner/App and owner/app are one repository.
CREATE UNIQUE INDEX workspace_sources_one_owner ON agentzaaheen.workspace_sources (tenant_id, kind, lower(ref));

CREATE TRIGGER workspace_sources_workspace_open BEFORE INSERT OR UPDATE ON agentzaaheen.workspace_sources
    FOR EACH ROW EXECUTE FUNCTION agentzaaheen.workspace_is_open();

ALTER TABLE agentzaaheen.workspace_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE agentzaaheen.workspace_sources FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_wall ON agentzaaheen.workspace_sources
    USING (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()))
    WITH CHECK (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()));

-- Deleting for good now also removes the workspace's repository link (same function, one more table).
CREATE OR REPLACE FUNCTION agentzaaheen.erase_workspace(
    target uuid, by_whom text, step timestamptz, at_time timestamptz
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    tenant uuid := agentzaaheen.current_tenant();
    t text;
BEGIN
    IF target IS DISTINCT FROM agentzaaheen.current_workspace() THEN
        RAISE EXCEPTION 'only the workspace this transaction is for' USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE agentzaaheen.workspaces
        SET state = 'deleted', state_changed_by = by_whom, state_changed_at = at_time, step_up_at = step
        WHERE tenant_id = tenant AND workspace_id = target AND state = 'archived';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'only an archived workspace can be deleted' USING ERRCODE = 'check_violation';
    END IF;
    FOREACH t IN ARRAY ARRAY['events', 'runs', 'findings', 'actions', 'sealed_secrets',
        'revenue_subscriptions', 'revenue_payments', 'revenue_refunds', 'source_syncs',
        'workspace_settings', 'agents', 'workspace_sources']
    LOOP
        EXECUTE format('DELETE FROM agentzaaheen.%I WHERE tenant_id = $1 AND project_id = $2', t)
            USING tenant, target;
    END LOOP;
END
$$;

REVOKE ALL ON agentzaaheen.workspace_sources FROM PUBLIC;
GRANT SELECT, INSERT ON agentzaaheen.workspace_sources TO agentzaaheen_app;
"""
