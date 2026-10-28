"""Migration 0019: custom MCP servers the Chairman added, and what their last test found (ADR-041 slice 1).

``custom_mcp_servers`` holds one row per server added on the Customize page's
MCP tab: its name and https address, who added it and when, and the last
test's result (when, the outcome code, the server's own name, its tools and
their fingerprint). Adding is only a request: there is no "approved" state
in this migration, so nothing here can ever be used by an agent or the chat.
Approval comes with slice 2 (fix forward with a new migration).

The tool names and descriptions are the server's own words, kept as plain
text and shown as untrusted. No key is ever kept here (slice 2 puts keys in
the vault).

Tenant-level, like the registry switches. The name and address never change
after adding; the app may set only the test columns, and mark a row removed
(rows are kept, as a record of what was added). One live row per name.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "custom_mcp"

SQL = r"""
CREATE TABLE agentzaaheen.custom_mcp_servers (
    id               uuid PRIMARY KEY,
    tenant_id        uuid NOT NULL,
    name             text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_-]{0,31}$'),
    url              text NOT NULL CHECK (url ~ '^https://[^/?#@\s]+(/[^?#\s]*)?$' AND char_length(url) <= 512),
    status           text NOT NULL CHECK (status IN ('requested', 'removed')),
    added_by         text NOT NULL CHECK (added_by ~ '^(user|system):[A-Za-z0-9_.-]{1,64}$'),
    added_at         timestamptz NOT NULL,
    removed_by       text CHECK (removed_by ~ '^(user|system):[A-Za-z0-9_.-]{1,64}$'),
    removed_at       timestamptz CHECK (removed_at IS NULL OR removed_at >= added_at),
    tested_at        timestamptz CHECK (tested_at IS NULL OR tested_at >= added_at),
    test_outcome     text CHECK (test_outcome ~ '^mcp\.[a-z_]{1,32}$'),
    server_name      text CHECK (char_length(server_name) <= 128),
    tools            jsonb CHECK (tools IS NULL OR (jsonb_typeof(tools) = 'array'
                         AND jsonb_array_length(tools) <= 500 AND octet_length(tools::text) <= 2097152)),
    tools_skipped    integer CHECK (tools_skipped IS NULL OR tools_skipped >= 0),
    tools_fingerprint text CHECK (tools_fingerprint ~ '^[0-9a-f]{64}$'),
    UNIQUE (tenant_id, id),
    -- Removed means someone removed it, at a time; otherwise neither is set.
    CHECK ((status = 'removed') = (removed_by IS NOT NULL AND removed_at IS NOT NULL)),
    -- A test is either not run yet (nothing set) or has a time and an outcome.
    CHECK ((tested_at IS NULL) = (test_outcome IS NULL)),
    -- Tools are kept only with their fingerprint, and only after a test that listed them.
    CHECK ((tools IS NULL) = (tools_fingerprint IS NULL)),
    CHECK (tools IS NULL OR test_outcome = 'mcp.listed')
);
-- One live server per name.
CREATE UNIQUE INDEX custom_mcp_servers_one_live ON agentzaaheen.custom_mcp_servers (tenant_id, name)
    WHERE status <> 'removed';
CREATE INDEX custom_mcp_servers_by_tenant ON agentzaaheen.custom_mcp_servers (tenant_id, added_at);

-- A removed server stays removed, and is never tested again.
CREATE FUNCTION agentzaaheen.custom_mcp_removed_stays() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF OLD.status = 'removed' THEN
        RAISE EXCEPTION 'a removed custom MCP server cannot change' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER custom_mcp_removed_check BEFORE UPDATE ON agentzaaheen.custom_mcp_servers
    FOR EACH ROW EXECUTE FUNCTION agentzaaheen.custom_mcp_removed_stays();

ALTER TABLE agentzaaheen.custom_mcp_servers ENABLE ROW LEVEL SECURITY;
ALTER TABLE agentzaaheen.custom_mcp_servers FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_wall ON agentzaaheen.custom_mcp_servers
    USING (tenant_id = (SELECT agentzaaheen.current_tenant()))
    WITH CHECK (tenant_id = (SELECT agentzaaheen.current_tenant()));

REVOKE ALL ON agentzaaheen.custom_mcp_servers FROM PUBLIC;
REVOKE ALL ON FUNCTION agentzaaheen.custom_mcp_removed_stays() FROM PUBLIC;
GRANT SELECT, INSERT ON agentzaaheen.custom_mcp_servers TO agentzaaheen_app;
GRANT UPDATE (status, removed_by, removed_at, tested_at, test_outcome, server_name, tools, tools_skipped,
    tools_fingerprint) ON agentzaaheen.custom_mcp_servers TO agentzaaheen_app;
"""
