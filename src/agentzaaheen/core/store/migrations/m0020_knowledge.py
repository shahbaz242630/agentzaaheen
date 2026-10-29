"""Migration 0020: the knowledge bank's first section, ``products``, per workspace (ADR-037, F12 slice 1).

``knowledge_pieces`` holds short passages cut from a project's own product
documents (its README, product brief, website pages), each with the file it
came from, that file's git blob id and when it was read, so an answer can name
its source and a changed file is replaced as a whole. ``words`` is Postgres's
own full-text index of the passage (English stemming, a heading's words
counting more than the text's): slice 1 searches by
words, and embeddings (pgvector) come only if the scored test set shows word
search missing answers (ADR-037 amendment, 29 Oct 2026).

Passages sit behind the workspace walls like every other workspace row: a
chat in one project can never find another project's documents. Only the
``products`` section exists yet; ``company``, ``lessons`` and the locked
``customers`` come in later slices. The app may read, add and delete (a
changed or unlisted file is replaced as a whole: these rows are copies of
documents kept in git, never the only record). Deleting a workspace for good
removes its passages.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "knowledge_pieces"

SQL = r"""
CREATE TABLE agentzaaheen.knowledge_pieces (
    tenant_id   uuid NOT NULL,
    project_id  uuid NOT NULL,
    section     text NOT NULL CHECK (section IN ('products')),
    source      text NOT NULL CHECK (source ~ '^[A-Za-z0-9_.][A-Za-z0-9_./-]{0,199}$' AND source !~ '\.\.'),
    blob_sha    text NOT NULL CHECK (blob_sha ~ '^[0-9a-f]{40}$'),
    position    integer NOT NULL CHECK (position BETWEEN 0 AND 999),
    title       text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    body        text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 4000),
    read_at     timestamptz NOT NULL,
    words       tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', title), 'A') || setweight(to_tsvector('english', body), 'B')
    ) STORED,
    PRIMARY KEY (tenant_id, project_id, section, source, position),
    FOREIGN KEY (tenant_id, project_id) REFERENCES agentzaaheen.workspaces (tenant_id, workspace_id)
);

CREATE INDEX knowledge_pieces_words ON agentzaaheen.knowledge_pieces USING gin (words);

CREATE TRIGGER knowledge_pieces_workspace_open BEFORE INSERT OR UPDATE ON agentzaaheen.knowledge_pieces
    FOR EACH ROW EXECUTE FUNCTION agentzaaheen.workspace_is_open();

ALTER TABLE agentzaaheen.knowledge_pieces ENABLE ROW LEVEL SECURITY;
ALTER TABLE agentzaaheen.knowledge_pieces FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_wall ON agentzaaheen.knowledge_pieces
    USING (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()))
    WITH CHECK (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()));

-- Deleting for good: the knowledge too.
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
    FOREACH t IN ARRAY ARRAY['chat_turns', 'chat_conversations', 'events', 'findings', 'actions', 'bookmarks',
        'runs', 'run_starts', 'sealed_secrets', 'revenue_subscriptions', 'revenue_payments', 'revenue_refunds',
        'source_syncs', 'workspace_settings', 'agents', 'workspace_sources', 'scout_runs', 'daily_summaries',
        'customer_name_prints', 'knowledge_pieces']
    LOOP
        EXECUTE format('DELETE FROM agentzaaheen.%I WHERE tenant_id = $1 AND project_id = $2', t)
            USING tenant, target;
    END LOOP;
END
$$;

REVOKE ALL ON agentzaaheen.knowledge_pieces FROM PUBLIC;
GRANT SELECT, INSERT, DELETE ON agentzaaheen.knowledge_pieces TO agentzaaheen_app;
"""
