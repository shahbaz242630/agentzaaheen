"""Migration 0016: a run is written down before it starts, and Claude calls name their run (F11 slice 1, ADR-036).

``run_starts`` holds one row per agent run, written by the runner **before** the
agent does anything: which agent, which frozen role version, when it started
and by when it must have ended (``deadline``: its time budget plus a little
for the closing writes). The run's end is the ``runs`` row with the same id.
A start with no end after its deadline is a run that died halfway (a laptop
that slept, a server restart): it is closed as ``could_not_run`` with the
code ``run.interrupted``, so the morning briefing names it.

``model_calls.run_id`` says which run a Claude call was for, so a run's cost
can be added up from the calls it made (the spend caps read the runs).
Calls made before this migration have none.

An index lets the daily summary count, from the audit log, the runs the
runner did not start because they were not meant to (switched off, paused,
stopped by a stop switch).

Deleting a workspace for good now also removes its run starts, and removes
findings and actions **before** the runs they point to (the old order would
have failed once any finding names its run).

Project-level, behind the workspace wall; nothing new lands in an archived
workspace. Append-only for the app.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "run_starts"

SQL = r"""
CREATE TABLE agentzaaheen.run_starts (
    tenant_id  uuid NOT NULL,
    project_id uuid NOT NULL,
    run_id     uuid NOT NULL,
    agent_id   uuid NOT NULL,
    role_id    text NOT NULL CHECK (role_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND char_length(role_id) <= 64),
    version    text NOT NULL CHECK (version ~ '^[0-9a-f]{64}$'),
    started_at timestamptz NOT NULL,
    deadline   timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, run_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES agentzaaheen.workspaces (tenant_id, workspace_id),
    CHECK (deadline > started_at)
);
CREATE INDEX run_starts_by_deadline ON agentzaaheen.run_starts (tenant_id, project_id, deadline);

CREATE TRIGGER run_starts_workspace_open BEFORE INSERT OR UPDATE ON agentzaaheen.run_starts
    FOR EACH ROW EXECUTE FUNCTION agentzaaheen.workspace_is_open();

ALTER TABLE agentzaaheen.run_starts ENABLE ROW LEVEL SECURITY;
ALTER TABLE agentzaaheen.run_starts FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_wall ON agentzaaheen.run_starts
    USING (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()))
    WITH CHECK (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()));

-- The daily summary counts runs the runner did not start (switched off, paused, stopped) from the audit log.
CREATE INDEX audit_log_by_workspace_action ON agentzaaheen.audit_log (tenant_id, project_id, action, at);

ALTER TABLE agentzaaheen.model_calls
    ADD COLUMN run_id text CHECK (run_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$');
CREATE INDEX model_calls_by_run ON agentzaaheen.model_calls (tenant_id, run_id) WHERE run_id IS NOT NULL;

-- Deleting for good: run starts too, and findings and actions before the runs they point to.
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
    FOREACH t IN ARRAY ARRAY['events', 'findings', 'actions', 'runs', 'run_starts', 'sealed_secrets',
        'revenue_subscriptions', 'revenue_payments', 'revenue_refunds', 'source_syncs',
        'workspace_settings', 'agents', 'workspace_sources', 'scout_runs', 'daily_summaries',
        'customer_name_prints']
    LOOP
        EXECUTE format('DELETE FROM agentzaaheen.%I WHERE tenant_id = $1 AND project_id = $2', t)
            USING tenant, target;
    END LOOP;
END
$$;

REVOKE ALL ON agentzaaheen.run_starts FROM PUBLIC;
GRANT SELECT, INSERT ON agentzaaheen.run_starts TO agentzaaheen_app;
"""
