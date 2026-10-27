"""Migration 0018: every chat kept as a record, per workspace (F19 slice 1, ADR-039, ADR-SEC-009).

``chat_conversations`` holds one row per conversation in a workspace: when it
started, when it was last used, and whether it was ended on purpose (the New
chat button or signing out; F19 slices 2-3). A conversation left alone for
3 hours is simply not continued: the next question starts a new one.
``cleared_for_learning`` starts false and stays false until a person reviews
the chat (F12): nothing is ever used to teach a model before that.

``chat_turns`` holds each question and its answer, numbered within the
conversation, with the model that answered and whether it saw sample data.
The words are **sealed by the vault** before they reach the database
(Chairman, 27 Oct 2026): each question and each answer has its own data key,
bound to its workspace, conversation and turn, so the database, its backups
and anyone reading them past the app see only sealed bytes. Sealed values are
the vault's own JSON (``SealedSecret``), so a later master-key rotation can
re-wrap them.

The Board's chat (no project open) is kept in Agent Zaaheen's own workspace,
so every chat sits behind a workspace wall; ``for_board`` keeps its
conversations apart from that project's own chat. Nothing new lands in an archived
workspace. The app may read and add, and move a conversation's last use or
end it, never delete: chats older than 12 months are removed by
``forget_old_chats`` (owner function, one workspace at a time), and deleting a
workspace for good removes its chats.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "chat_records"

SQL = r"""
CREATE TABLE agentzaaheen.chat_conversations (
    tenant_id            uuid NOT NULL,
    project_id           uuid NOT NULL,
    conversation_id      uuid NOT NULL,
    started_at           timestamptz NOT NULL,
    last_at              timestamptz NOT NULL CHECK (last_at >= started_at),
    ended_at             timestamptz CHECK (ended_at IS NULL OR ended_at >= started_at),
    ended_why            text CHECK (ended_why IN ('new_chat', 'signed_out')),
    for_board            boolean NOT NULL,
    cleared_for_learning boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id, project_id, conversation_id),
    FOREIGN KEY (tenant_id, project_id) REFERENCES agentzaaheen.workspaces (tenant_id, workspace_id),
    CHECK ((ended_at IS NULL) = (ended_why IS NULL))
);

CREATE INDEX chat_conversations_by_last ON agentzaaheen.chat_conversations (tenant_id, project_id, for_board, last_at);

CREATE TABLE agentzaaheen.chat_turns (
    tenant_id       uuid NOT NULL,
    project_id      uuid NOT NULL,
    conversation_id uuid NOT NULL,
    turn            integer NOT NULL CHECK (turn >= 1),
    at              timestamptz NOT NULL,
    question_sealed bytea NOT NULL CHECK (octet_length(question_sealed) BETWEEN 1 AND 32768),
    answer_sealed   bytea NOT NULL CHECK (octet_length(answer_sealed) BETWEEN 1 AND 32768),
    model_id        text NOT NULL CHECK (model_id ~ '^[a-z0-9][a-z0-9.-]{0,63}$'),
    from_sample     boolean NOT NULL,
    PRIMARY KEY (tenant_id, project_id, conversation_id, turn),
    FOREIGN KEY (tenant_id, project_id, conversation_id)
        REFERENCES agentzaaheen.chat_conversations (tenant_id, project_id, conversation_id)
);

CREATE TRIGGER chat_conversations_workspace_open BEFORE INSERT OR UPDATE ON agentzaaheen.chat_conversations
    FOR EACH ROW EXECUTE FUNCTION agentzaaheen.workspace_is_open();
CREATE TRIGGER chat_turns_workspace_open BEFORE INSERT OR UPDATE ON agentzaaheen.chat_turns
    FOR EACH ROW EXECUTE FUNCTION agentzaaheen.workspace_is_open();

ALTER TABLE agentzaaheen.chat_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE agentzaaheen.chat_conversations FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_wall ON agentzaaheen.chat_conversations
    USING (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()))
    WITH CHECK (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()));

ALTER TABLE agentzaaheen.chat_turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE agentzaaheen.chat_turns FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_wall ON agentzaaheen.chat_turns
    USING (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()))
    WITH CHECK (tenant_id = (SELECT agentzaaheen.current_tenant())
        AND project_id = (SELECT agentzaaheen.current_workspace()));

-- Chats older than the cut-off, in the workspace this transaction is for: turns first, then their
-- conversations. Runs as the owner (the app may not delete), still behind the walls. Returns how many
-- conversations went.
CREATE FUNCTION agentzaaheen.forget_old_chats(before timestamptz) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    tenant uuid := agentzaaheen.current_tenant();
    target uuid := agentzaaheen.current_workspace();
    gone integer;
BEGIN
    DELETE FROM agentzaaheen.chat_turns t
        USING agentzaaheen.chat_conversations c
        WHERE t.tenant_id = tenant AND t.project_id = target
          AND c.tenant_id = t.tenant_id AND c.project_id = t.project_id AND c.conversation_id = t.conversation_id
          AND c.last_at < before;
    DELETE FROM agentzaaheen.chat_conversations
        WHERE tenant_id = tenant AND project_id = target AND last_at < before;
    GET DIAGNOSTICS gone = ROW_COUNT;
    RETURN gone;
END
$$;

-- Deleting for good: chats too (turns before their conversations).
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
        'customer_name_prints']
    LOOP
        EXECUTE format('DELETE FROM agentzaaheen.%I WHERE tenant_id = $1 AND project_id = $2', t)
            USING tenant, target;
    END LOOP;
END
$$;

REVOKE ALL ON FUNCTION agentzaaheen.forget_old_chats(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agentzaaheen.forget_old_chats(timestamptz) TO agentzaaheen_app;

REVOKE ALL ON agentzaaheen.chat_conversations FROM PUBLIC;
GRANT SELECT, INSERT ON agentzaaheen.chat_conversations TO agentzaaheen_app;
GRANT UPDATE (last_at, ended_at, ended_why) ON agentzaaheen.chat_conversations TO agentzaaheen_app;
REVOKE ALL ON agentzaaheen.chat_turns FROM PUBLIC;
GRANT SELECT, INSERT ON agentzaaheen.chat_turns TO agentzaaheen_app;
"""
