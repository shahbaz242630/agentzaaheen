"""Migration 0007: agents per workspace, frozen role versions, switches and spend caps (F17 slice 3, ADR-039).

A role pack lives in a folder outside the repository (the role library). When
a role is given to a workspace, the pack's text is stored here once, under its
SHA-256 fingerprint, and never changes: the agent runs that frozen copy until
someone moves it to a newer one on purpose.

Safety rules the database keeps, whatever the code above it does:
- an agent is created switched off;
- switching an agent on, raising its cap, switching a workspace back on or
  raising a workspace's cap all need a fresh passkey step-up (``step_up_at``
  moves forward with the change);
- switching off, lowering a cap or removing it need nothing: stopping is always allowed.
  No cap (NULL) means nothing may be spent, so removing a cap is the safest change.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "agents"

SQL = r"""
-- Frozen copies of role packs: written once, never changed or deleted.
CREATE TABLE yaqzan.role_versions (
    tenant_id  uuid NOT NULL,
    role_id    text NOT NULL CHECK (role_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND char_length(role_id) <= 64),
    version    text NOT NULL CHECK (version ~ '^[0-9a-f]{64}$'),
    files      jsonb NOT NULL CHECK (jsonb_typeof(files) = 'object'),
    permission text NOT NULL CHECK (permission IN ('read', 'draft', 'act')),
    stored_at  timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, role_id, version)
);

-- One row per workspace: its on/off switch and its monthly spending cap (minor units of USD).
CREATE TABLE yaqzan.workspace_settings (
    tenant_id   uuid NOT NULL,
    project_id  uuid NOT NULL,
    enabled     boolean NOT NULL,
    cap_minor   bigint CHECK (cap_minor >= 0),
    changed_by  text NOT NULL CHECK (changed_by ~ '^(user|system):[A-Za-z0-9_.-]{1,64}$'),
    changed_at  timestamptz NOT NULL,
    step_up_at  timestamptz,
    PRIMARY KEY (tenant_id, project_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES yaqzan.workspaces (tenant_id, workspace_id)
);

-- One agent per role per workspace, running one frozen role version.
CREATE TABLE yaqzan.agents (
    tenant_id   uuid NOT NULL,
    project_id  uuid NOT NULL,
    agent_id    uuid NOT NULL,
    role_id     text NOT NULL,
    version     text NOT NULL,
    enabled     boolean NOT NULL,
    model       text NOT NULL CHECK (model ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND char_length(model) <= 40),
    cap_minor   bigint CHECK (cap_minor >= 0),
    created_by  text NOT NULL CHECK (created_by ~ '^(user|system):[A-Za-z0-9_.-]{1,64}$'),
    created_at  timestamptz NOT NULL,
    changed_by  text NOT NULL CHECK (changed_by ~ '^(user|system):[A-Za-z0-9_.-]{1,64}$'),
    changed_at  timestamptz NOT NULL,
    step_up_at  timestamptz,
    PRIMARY KEY (tenant_id, project_id, agent_id),
    UNIQUE (tenant_id, project_id, role_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES yaqzan.workspaces (tenant_id, workspace_id),
    FOREIGN KEY (tenant_id, role_id, version) REFERENCES yaqzan.role_versions (tenant_id, role_id, version),
    -- Only a person switches an agent on, and only with a passkey step-up.
    CHECK (NOT enabled OR (step_up_at IS NOT NULL AND changed_by LIKE 'user:%'))
);
CREATE INDEX runs_by_agent ON yaqzan.runs (tenant_id, project_id, agent, started_at);

-- An agent starts switched off.
CREATE FUNCTION yaqzan.agent_starts_off() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF NEW.enabled THEN
        RAISE EXCEPTION 'a new agent starts switched off' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER agent_starts_off_check BEFORE INSERT ON yaqzan.agents
    FOR EACH ROW EXECUTE FUNCTION yaqzan.agent_starts_off();

-- Going riskier (on, or a higher cap) needs a new step-up; going safer needs nothing.
-- What an agent is (its workspace, id, role, creation) never changes after it is made.
CREATE FUNCTION yaqzan.agent_change_check() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    riskier boolean;
BEGIN
    IF (NEW.tenant_id, NEW.project_id, NEW.agent_id, NEW.role_id, NEW.created_by, NEW.created_at)
        IS DISTINCT FROM
       (OLD.tenant_id, OLD.project_id, OLD.agent_id, OLD.role_id, OLD.created_by, OLD.created_at) THEN
        RAISE EXCEPTION 'what an agent is cannot change' USING ERRCODE = 'check_violation';
    END IF;
    riskier := (NEW.enabled AND NOT OLD.enabled)
        OR (NEW.cap_minor IS NOT NULL AND (OLD.cap_minor IS NULL OR NEW.cap_minor > OLD.cap_minor))
        OR (NEW.version <> OLD.version)
        OR (NEW.model <> OLD.model);
    IF riskier AND (NEW.step_up_at IS NULL OR NEW.step_up_at IS NOT DISTINCT FROM OLD.step_up_at
            OR NEW.changed_by NOT LIKE 'user:%') THEN
        RAISE EXCEPTION 'this change needs a fresh passkey step-up' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER agent_change BEFORE UPDATE ON yaqzan.agents
    FOR EACH ROW EXECUTE FUNCTION yaqzan.agent_change_check();

-- The same for a workspace: back on, or a higher cap, needs a fresh step-up.
CREATE FUNCTION yaqzan.workspace_settings_check() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    riskier boolean;
    before_enabled boolean := false;
    before_cap bigint := NULL;
    before_step timestamptz := NULL;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.tenant_id, NEW.project_id) IS DISTINCT FROM (OLD.tenant_id, OLD.project_id) THEN
            RAISE EXCEPTION 'a workspace setting cannot move to another workspace'
                USING ERRCODE = 'check_violation';
        END IF;
        before_enabled := OLD.enabled;
        before_cap := OLD.cap_minor;
        before_step := OLD.step_up_at;
    END IF;
    riskier := (NEW.enabled AND NOT before_enabled)
        OR (NEW.cap_minor IS NOT NULL AND (before_cap IS NULL OR NEW.cap_minor > before_cap));
    IF riskier AND (NEW.step_up_at IS NULL OR NEW.step_up_at IS NOT DISTINCT FROM before_step
            OR NEW.changed_by NOT LIKE 'user:%') THEN
        RAISE EXCEPTION 'this change needs a fresh passkey step-up' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER workspace_settings_change BEFORE INSERT OR UPDATE ON yaqzan.workspace_settings
    FOR EACH ROW EXECUTE FUNCTION yaqzan.workspace_settings_check();

-- The walls (ADR-039): role versions are the tenant's; agents and settings belong to one workspace.
ALTER TABLE yaqzan.role_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE yaqzan.role_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_wall ON yaqzan.role_versions
    USING (tenant_id = (SELECT yaqzan.current_tenant()))
    WITH CHECK (tenant_id = (SELECT yaqzan.current_tenant()));
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['workspace_settings', 'agents']
    LOOP
        EXECUTE format('ALTER TABLE yaqzan.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE yaqzan.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY workspace_wall ON yaqzan.%I'
            ' USING (tenant_id = (SELECT yaqzan.current_tenant())'
            ' AND project_id = (SELECT yaqzan.current_workspace()))'
            ' WITH CHECK (tenant_id = (SELECT yaqzan.current_tenant())'
            ' AND project_id = (SELECT yaqzan.current_workspace()))', t);
    END LOOP;
END
$$;

REVOKE ALL ON yaqzan.role_versions, yaqzan.workspace_settings, yaqzan.agents FROM PUBLIC;
REVOKE ALL ON FUNCTION yaqzan.agent_starts_off(), yaqzan.agent_change_check(),
    yaqzan.workspace_settings_check() FROM PUBLIC;
GRANT SELECT, INSERT ON yaqzan.role_versions TO yaqzan_app;
GRANT SELECT, INSERT, UPDATE ON yaqzan.workspace_settings, yaqzan.agents TO yaqzan_app;
"""
