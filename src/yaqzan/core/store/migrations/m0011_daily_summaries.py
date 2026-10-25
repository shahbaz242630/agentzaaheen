"""Migration 0011: a daily summary per workspace, and the Board's morning briefing (F17 slice 6, ADR-039).

Each morning every active workspace gets one summary of its last day:
``daily_summaries`` holds its facts as checked JSON (written by code from what
Yaqzan already holds, never by a model), one row per workspace per day; making
that day's summary again replaces it. The row is project-level, behind the
same wall as the rest; nothing lands in an archived workspace; deleting a
workspace for good removes its summaries too (archiving keeps them).

``briefings`` is the Board's: one per tenant per day, the exact message sent
to the Chairman's phone and whether it went (``waiting``, ``sent``, or
``not_sent`` with a reason code, which a later try may still turn into
``sent``). It is tenant-level, behind the tenant wall.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "daily_summaries"

SQL = r"""
CREATE TABLE yaqzan.daily_summaries (
    tenant_id  uuid NOT NULL,
    project_id uuid NOT NULL,
    day        date NOT NULL,
    made_at    timestamptz NOT NULL,
    facts      jsonb NOT NULL CHECK (jsonb_typeof(facts) = 'object'),
    PRIMARY KEY (tenant_id, project_id, day),
    FOREIGN KEY (tenant_id, project_id) REFERENCES yaqzan.workspaces (tenant_id, workspace_id)
);

CREATE TRIGGER daily_summaries_workspace_open BEFORE INSERT OR UPDATE ON yaqzan.daily_summaries
    FOR EACH ROW EXECUTE FUNCTION yaqzan.workspace_is_open();

ALTER TABLE yaqzan.daily_summaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE yaqzan.daily_summaries FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_wall ON yaqzan.daily_summaries
    USING (tenant_id = (SELECT yaqzan.current_tenant())
        AND project_id = (SELECT yaqzan.current_workspace()))
    WITH CHECK (tenant_id = (SELECT yaqzan.current_tenant())
        AND project_id = (SELECT yaqzan.current_workspace()));

CREATE TABLE yaqzan.briefings (
    tenant_id uuid NOT NULL,
    day       date NOT NULL,
    made_at   timestamptz NOT NULL,
    title     text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    message   text NOT NULL CHECK (length(message) BETWEEN 1 AND 3500),
    state     text NOT NULL CHECK (state IN ('waiting', 'sent', 'not_sent')),
    sent_at   timestamptz,
    reason    text CHECK (reason ~ '^[a-z0-9_]+(\.[a-z0-9_]+)*$'),
    PRIMARY KEY (tenant_id, day),
    -- Sent has a time and no reason; not sent has a reason and no time; waiting has neither.
    CHECK ((state = 'sent') = (sent_at IS NOT NULL)),
    CHECK ((state = 'not_sent') = (reason IS NOT NULL)),
    CHECK (sent_at IS NULL OR sent_at >= made_at)
);

ALTER TABLE yaqzan.briefings ENABLE ROW LEVEL SECURITY;
ALTER TABLE yaqzan.briefings FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_wall ON yaqzan.briefings
    USING (tenant_id = (SELECT yaqzan.current_tenant()))
    WITH CHECK (tenant_id = (SELECT yaqzan.current_tenant()));

-- Deleting for good now also removes the workspace's daily summaries (same function, one more table).
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
        'workspace_settings', 'agents', 'workspace_sources', 'scout_runs', 'daily_summaries']
    LOOP
        EXECUTE format('DELETE FROM yaqzan.%I WHERE tenant_id = $1 AND project_id = $2', t)
            USING tenant, target;
    END LOOP;
END
$$;

REVOKE ALL ON yaqzan.daily_summaries FROM PUBLIC;
GRANT SELECT, INSERT ON yaqzan.daily_summaries TO yaqzan_app;
GRANT UPDATE (made_at, facts) ON yaqzan.daily_summaries TO yaqzan_app;

REVOKE ALL ON yaqzan.briefings FROM PUBLIC;
GRANT SELECT, INSERT ON yaqzan.briefings TO yaqzan_app;
GRANT UPDATE (state, sent_at, reason) ON yaqzan.briefings TO yaqzan_app;
"""
