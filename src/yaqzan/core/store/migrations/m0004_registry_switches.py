"""Migration 0004: registry switches and the requests to switch an item on (F06 slice 2b, ADR-030).

Kept apart from the manifests, so the app never edits a manifest (ADR-027).
"Off" may be written at any time. "On" may only point at an approved request
for exactly the same item and files (fingerprint), and an approval must carry
the time of its passkey step-up: the database refuses anything else, whatever
the code above it does.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "registry_switches"

SQL = r"""
CREATE TABLE yaqzan.registry_requests (
    id           uuid PRIMARY KEY,
    tenant_id    uuid NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('connector', 'skill', 'plugin')),
    item_id      text NOT NULL CHECK (item_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND char_length(item_id) <= 64),
    fingerprint  text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    status       text NOT NULL CHECK (status IN ('pending', 'approved', 'refused', 'withdrawn')),
    requested_by text NOT NULL CHECK (requested_by ~ '^(user|system):[A-Za-z0-9_.-]{1,64}$'),
    requested_at timestamptz NOT NULL,
    decided_by   text CHECK (decided_by ~ '^(user|system):[A-Za-z0-9_.-]{1,64}$'),
    decided_at   timestamptz,
    step_up_at   timestamptz,
    UNIQUE (tenant_id, id),
    -- Pending means undecided; anything else names who decided and when.
    CHECK ((status = 'pending') = (decided_by IS NULL AND decided_at IS NULL)),
    -- Only a person approves, and only with a passkey step-up.
    CHECK (status <> 'approved' OR (step_up_at IS NOT NULL AND decided_by LIKE 'user:%'))
);
-- At most one open request per item.
CREATE UNIQUE INDEX registry_requests_one_pending ON yaqzan.registry_requests (tenant_id, kind, item_id)
    WHERE status = 'pending';
CREATE INDEX registry_requests_by_tenant ON yaqzan.registry_requests (tenant_id, requested_at DESC);

-- A decision is final, and what was asked for never changes after the asking.
CREATE FUNCTION yaqzan.registry_request_decided_once() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'a decided registry request cannot change' USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.id, NEW.tenant_id, NEW.kind, NEW.item_id, NEW.fingerprint, NEW.requested_by,
            NEW.requested_at)
        IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.kind, OLD.item_id, OLD.fingerprint, OLD.requested_by,
            OLD.requested_at) THEN
        RAISE EXCEPTION 'only the decision of a registry request may be set'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER registry_request_decide BEFORE UPDATE ON yaqzan.registry_requests
    FOR EACH ROW EXECUTE FUNCTION yaqzan.registry_request_decided_once();

CREATE TABLE yaqzan.registry_switches (
    tenant_id   uuid NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('connector', 'skill', 'plugin')),
    item_id     text NOT NULL CHECK (item_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND char_length(item_id) <= 64),
    enabled     boolean NOT NULL,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    request_id  uuid,
    changed_by  text NOT NULL CHECK (changed_by ~ '^(user|system):[A-Za-z0-9_.-]{1,64}$'),
    changed_at  timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, kind, item_id),
    FOREIGN KEY (tenant_id, request_id) REFERENCES yaqzan.registry_requests (tenant_id, id),
    -- "On" always names the request that was approved; "off" never does.
    CHECK (enabled = (request_id IS NOT NULL))
);

-- "On" must point at an approved request for this very item and these very files.
CREATE FUNCTION yaqzan.registry_switch_on_needs_approval() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    approved boolean;
BEGIN
    IF NOT NEW.enabled THEN
        RETURN NEW;
    END IF;
    SELECT true INTO approved FROM yaqzan.registry_requests r
        WHERE r.tenant_id = NEW.tenant_id AND r.id = NEW.request_id AND r.status = 'approved'
            AND r.kind = NEW.kind AND r.item_id = NEW.item_id AND r.fingerprint = NEW.fingerprint;
    IF approved IS NULL THEN
        RAISE EXCEPTION 'a registry item may only be switched on by an approved request for the same files'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER registry_switch_on_check BEFORE INSERT OR UPDATE ON yaqzan.registry_switches
    FOR EACH ROW EXECUTE FUNCTION yaqzan.registry_switch_on_needs_approval();

REVOKE ALL ON yaqzan.registry_requests, yaqzan.registry_switches FROM PUBLIC;
REVOKE ALL ON FUNCTION yaqzan.registry_switch_on_needs_approval(), yaqzan.registry_request_decided_once()
    FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON yaqzan.registry_requests, yaqzan.registry_switches TO yaqzan_app;
"""
