"""Migration 0017: bookmarks, where each agent got to, moved only with what it found (F11 slice 4, ADR-036).

``bookmarks`` holds one row per agent and bookmark name: the last thing that
agent has dealt with (Monitoring: how uptime stood when it last wrote it down,
so the same "down" is not reported every five minutes). A bookmark is moved
only in the same transaction that saves the run's end and its findings, after
the findings: if saving a finding fails, the bookmark stays where it was and
the next run reports it again. Nothing is lost between "seen" and "saved".

``run_id`` names the run that last moved it. Values are our own short codes and
fingerprints, never text from outside.

Project-level, behind the workspace wall; nothing new lands in an archived
workspace. The app may read, add and move a bookmark, never delete one
(deleting a workspace for good removes them).

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "bookmarks"

SQL = r"""
CREATE TABLE agentzaaheen.bookmarks (
    tenant_id  uuid NOT NULL,
    project_id uuid NOT NULL,
    agent_id   uuid NOT NULL,
    name       text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$' AND char_length(name) <= 64),
    value      text NOT NULL CHECK (char_length(value) BETWEEN 1 AND 256),
    run_id     uuid NOT NULL,
    moved_at   timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, project_id, agent_id, name),
    FOREIGN KEY (tenant_id, project_id) REFERENCES agentzaaheen.workspaces (tenant_id, workspace_id),
    FOREIGN KEY (run_id) REFERENCES agentzaaheen.runs (id)
);

CREATE TRIGGER bookmarks_workspace_open BEFORE INSERT OR UPDATE ON agentzaaheen.bookmarks
    FOR EACH ROW EXECUTE FUNCTION agentzaaheen.workspace_is_open();

ALTER TABLE agentzaaheen.bookmarks ENABLE ROW LEVEL SECURITY;
ALTER TABLE agentzaaheen.bookmarks FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_wall ON agentzaaheen.bookmarks
    USING (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()))
    WITH CHECK (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()));

-- Deleting for good: bookmarks too, before the runs they point to.
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
    FOREACH t IN ARRAY ARRAY['events', 'findings', 'actions', 'bookmarks', 'runs', 'run_starts',
        'sealed_secrets', 'revenue_subscriptions', 'revenue_payments', 'revenue_refunds', 'source_syncs',
        'workspace_settings', 'agents', 'workspace_sources', 'scout_runs', 'daily_summaries',
        'customer_name_prints']
    LOOP
        EXECUTE format('DELETE FROM agentzaaheen.%I WHERE tenant_id = $1 AND project_id = $2', t)
            USING tenant, target;
    END LOOP;
END
$$;

REVOKE ALL ON agentzaaheen.bookmarks FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON agentzaaheen.bookmarks TO agentzaaheen_app;
"""
