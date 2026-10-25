"""Migration 0008: archiving a workspace, and deleting one for good (F17 slice 4, ADR-039).

A workspace is ``active``, ``archived`` or ``deleted``.

Archiving disconnects everything in one transaction: its agents and its own
switch go off with no caps (nothing may be spent), its saved keys are deleted,
and from then on the database refuses any new or changed row in it. Its
history stays, so it can be brought back.

Deleting for good is only possible from ``archived`` (so it always takes two
separate passkey step-ups) and goes through :func:`agentzaaheen.erase_workspace`,
which removes every project-level row of that workspace. The workspace row
itself stays as a marker with ``deleted`` (no data), so nothing that sets up
workspaces can bring it back by accident. ``deleted`` is final. The audit log
is never touched: it keeps the record that the workspace existed and why it went.

Every change of state needs a fresh passkey step-up by a person.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "workspace_archive"

SQL = r"""
ALTER TABLE agentzaaheen.workspaces
    ADD COLUMN state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'archived', 'deleted')),
    ADD COLUMN state_changed_by text CHECK (state_changed_by ~ '^(user|system):[A-Za-z0-9_.-]{1,64}$'),
    ADD COLUMN state_changed_at timestamptz,
    ADD COLUMN step_up_at timestamptz;

-- Active -> archived -> active again, or archived -> deleted (final).
-- Each move needs a new step-up by a person.
CREATE FUNCTION agentzaaheen.workspace_state_check() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'active' THEN
            RAISE EXCEPTION 'a new workspace starts active' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.state IS NOT DISTINCT FROM OLD.state THEN
        IF OLD.state = 'deleted' AND NEW.name IS DISTINCT FROM OLD.name THEN
            RAISE EXCEPTION 'a deleted workspace cannot change' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT ((OLD.state = 'active' AND NEW.state = 'archived')
            OR (OLD.state = 'archived' AND NEW.state IN ('active', 'deleted'))) THEN
        RAISE EXCEPTION 'a workspace cannot go from % to %', OLD.state, NEW.state
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.step_up_at IS NULL OR NEW.step_up_at IS NOT DISTINCT FROM OLD.step_up_at
            OR NEW.state_changed_by IS NULL OR NEW.state_changed_by NOT LIKE 'user:%' THEN
        RAISE EXCEPTION 'this change needs a fresh passkey step-up' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER workspace_state_change BEFORE INSERT OR UPDATE ON agentzaaheen.workspaces
    FOR EACH ROW EXECUTE FUNCTION agentzaaheen.workspace_state_check();

-- Nothing new or changed lands in a workspace that is not active (deleting is a separate path).
-- A workspace this transaction cannot see is left to the walls and the foreign key, which refuse it.
CREATE FUNCTION agentzaaheen.workspace_is_open() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM agentzaaheen.workspaces w
               WHERE w.tenant_id = NEW.tenant_id AND w.workspace_id = NEW.project_id
                 AND w.state <> 'active') THEN
        RAISE EXCEPTION 'this workspace is archived or deleted' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['events', 'runs', 'findings', 'actions', 'sealed_secrets',
        'revenue_subscriptions', 'revenue_payments', 'revenue_refunds', 'source_syncs',
        'workspace_settings', 'agents']
    LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE ON agentzaaheen.%I'
            ' FOR EACH ROW EXECUTE FUNCTION agentzaaheen.workspace_is_open()', t || '_workspace_open', t);
    END LOOP;
END
$$;

-- Delete an archived workspace's data for good. Runs as the owner (the app may not delete history),
-- still behind the walls: only the workspace this transaction is scoped to, and only once archived.
CREATE FUNCTION agentzaaheen.erase_workspace(target uuid, by_whom text, step timestamptz, at_time timestamptz)
    RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
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
        'workspace_settings', 'agents']
    LOOP
        EXECUTE format('DELETE FROM agentzaaheen.%I WHERE tenant_id = $1 AND project_id = $2', t)
            USING tenant, target;
    END LOOP;
END
$$;

REVOKE ALL ON FUNCTION agentzaaheen.workspace_state_check(), agentzaaheen.workspace_is_open(),
    agentzaaheen.erase_workspace(uuid, text, timestamptz, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agentzaaheen.erase_workspace(uuid, text, timestamptz, timestamptz) TO agentzaaheen_app;
GRANT UPDATE (state, state_changed_by, state_changed_at, step_up_at) ON agentzaaheen.workspaces TO agentzaaheen_app;
"""
