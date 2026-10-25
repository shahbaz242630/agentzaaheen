"""Migration 0013: our customers' names as keyed fingerprints only (F10 slice 2b, ADR-023, ADR-033).

The customer-data check refuses any Claude call that holds one of our
customers' names. AgentZaaheen never keeps the names: each is turned into a keyed
fingerprint on the machine (``agentzaaheen.core.vault.NamePrinter``, its key in a
key file, never in the database), and only the fingerprint is stored here.
``kind`` says what it is of: a whole name (``full``), the first word of one
(``first``, only a hint to look further) or a one-word name (``single``).
``key_id`` names the key that made it, so prints from an older key are never
read as current ones.

Project-level, behind the workspace wall; nothing new lands in an archived
workspace. Append-only for the app: a customer who has left is still
protected. Deleting a workspace for good removes its prints too.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "customer_name_prints"

SQL = r"""
CREATE TABLE agentzaaheen.customer_name_prints (
    tenant_id  uuid NOT NULL,
    project_id uuid NOT NULL,
    key_id     text NOT NULL CHECK (key_id ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    kind       text NOT NULL CHECK (kind IN ('full', 'first', 'single')),
    print      bytea NOT NULL CHECK (length(print) = 32),
    added_at   timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, project_id, key_id, kind, print),
    FOREIGN KEY (tenant_id, project_id) REFERENCES agentzaaheen.workspaces (tenant_id, workspace_id)
);

CREATE TRIGGER customer_name_prints_workspace_open BEFORE INSERT OR UPDATE ON agentzaaheen.customer_name_prints
    FOR EACH ROW EXECUTE FUNCTION agentzaaheen.workspace_is_open();

ALTER TABLE agentzaaheen.customer_name_prints ENABLE ROW LEVEL SECURITY;
ALTER TABLE agentzaaheen.customer_name_prints FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_wall ON agentzaaheen.customer_name_prints
    USING (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()))
    WITH CHECK (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()));

-- Deleting for good now also removes the workspace's name prints (same function, one more table).
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
        'workspace_settings', 'agents', 'workspace_sources', 'scout_runs', 'daily_summaries',
        'customer_name_prints']
    LOOP
        EXECUTE format('DELETE FROM agentzaaheen.%I WHERE tenant_id = $1 AND project_id = $2', t)
            USING tenant, target;
    END LOOP;
END
$$;

REVOKE ALL ON agentzaaheen.customer_name_prints FROM PUBLIC;
GRANT SELECT, INSERT ON agentzaaheen.customer_name_prints TO agentzaaheen_app;
"""
