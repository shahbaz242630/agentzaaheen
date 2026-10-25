"""Migration 0010: the scout's runs and its checked advice, per workspace (F17 slice 5b-2, ADR-039).

The scout reads a workspace's repository on the local model and recommends,
role by role, which agents the project needs. Each run is one row: what it
read (the repository, its branch), which model, where it is (``stage``: reading,
thinking, checking), and how it ended: ``done`` with the **checked** advice in
``result`` (code has already removed made-up files and unknown roles), or
``failed`` with a reason code. The advice is only advice: nothing is switched
on and nothing spends because of it (slice 5c lets the Chairman act on it).

One run at a time per workspace (a partial unique index). The row is
project-level, behind the same wall as the rest; nothing lands in an archived
workspace; deleting a workspace for good removes its runs too.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "scout_runs"

SQL = r"""
CREATE TABLE yaqzan.scout_runs (
    tenant_id   uuid NOT NULL,
    project_id  uuid NOT NULL,
    run_id      uuid NOT NULL,
    repo        text NOT NULL CHECK (repo ~ '^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$'),
    branch      text CHECK (branch ~ '^[A-Za-z0-9._/-]{1,255}$'),
    model       text NOT NULL CHECK (model ~ '^[a-z0-9][a-z0-9.-]{0,63}$'),
    state       text NOT NULL CHECK (state IN ('running', 'done', 'failed')),
    stage       text NOT NULL CHECK (stage IN ('reading', 'thinking', 'checking', 'finished')),
    started_by  text NOT NULL CHECK (started_by ~ '^(user|system):[A-Za-z0-9_.-]{1,64}$'),
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    reason      text CHECK (reason ~ '^[a-z0-9_]+(\.[a-z0-9_]+)*$'),
    result      jsonb CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
    PRIMARY KEY (tenant_id, run_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES yaqzan.workspaces (tenant_id, workspace_id),
    -- How a run ends is all or nothing: done has advice, failed has a reason, running has neither.
    CHECK ((state = 'running') = (finished_at IS NULL)),
    CHECK ((state = 'done') = (result IS NOT NULL)),
    CHECK ((state = 'failed') = (reason IS NOT NULL)),
    CHECK ((state = 'running') = (stage <> 'finished')),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);
CREATE UNIQUE INDEX scout_runs_one_at_a_time ON yaqzan.scout_runs (tenant_id, project_id)
    WHERE state = 'running';
CREATE INDEX scout_runs_latest ON yaqzan.scout_runs (tenant_id, project_id, started_at DESC);

CREATE TRIGGER scout_runs_workspace_open BEFORE INSERT OR UPDATE ON yaqzan.scout_runs
    FOR EACH ROW EXECUTE FUNCTION yaqzan.workspace_is_open();

ALTER TABLE yaqzan.scout_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE yaqzan.scout_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_wall ON yaqzan.scout_runs
    USING (tenant_id = (SELECT yaqzan.current_tenant())
        AND project_id = (SELECT yaqzan.current_workspace()))
    WITH CHECK (tenant_id = (SELECT yaqzan.current_tenant())
        AND project_id = (SELECT yaqzan.current_workspace()));

-- Deleting for good now also removes the workspace's scout runs (same function, one more table).
CREATE OR REPLACE FUNCTION yaqzan.erase_workspace(
    target uuid, by_whom text, step timestamptz, at_time timestamptz
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    tenant uuid := yaqzan.current_tenant();
    t text;
BEGIN
    IF target IS DISTINCT FROM yaqzan.current_workspace() THEN
        RAISE EXCEPTION 'only the workspace this transaction is for' USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE yaqzan.workspaces
        SET state = 'deleted', state_changed_by = by_whom, state_changed_at = at_time, step_up_at = step
        WHERE tenant_id = tenant AND workspace_id = target AND state = 'archived';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'only an archived workspace can be deleted' USING ERRCODE = 'check_violation';
    END IF;
    FOREACH t IN ARRAY ARRAY['events', 'runs', 'findings', 'actions', 'sealed_secrets',
        'revenue_subscriptions', 'revenue_payments', 'revenue_refunds', 'source_syncs',
        'workspace_settings', 'agents', 'workspace_sources', 'scout_runs']
    LOOP
        EXECUTE format('DELETE FROM yaqzan.%I WHERE tenant_id = $1 AND project_id = $2', t)
            USING tenant, target;
    END LOOP;
END
$$;

REVOKE ALL ON yaqzan.scout_runs FROM PUBLIC;
GRANT SELECT, INSERT ON yaqzan.scout_runs TO yaqzan_app;
GRANT UPDATE (branch, state, stage, finished_at, reason, result) ON yaqzan.scout_runs TO yaqzan_app;
"""
