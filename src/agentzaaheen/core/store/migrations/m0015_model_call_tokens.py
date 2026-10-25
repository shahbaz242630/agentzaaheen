"""Migration 0015: each Claude call's token counts, to measure what Opus's thinking costs (F10 slice 3b, ADR-033).

Opus 5.5 always thinks; its thinking comes back empty but is billed as output.
To set thinking from evidence (ADR-033: "measure the first 20 runs"), each row
of ``model_calls`` now also keeps the call's token counts and the length of the
visible answer in characters. Counts only: still no prompt and no answer.

The columns are empty for calls made before this migration. They are filled
once, on insert; the table stays append-only (no UPDATE grant).

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "model_call_tokens"

SQL = r"""
ALTER TABLE agentzaaheen.model_calls
    ADD COLUMN tokens_in    integer CHECK (tokens_in >= 0),
    ADD COLUMN tokens_out   integer CHECK (tokens_out >= 0),
    ADD COLUMN cache_read   integer CHECK (cache_read >= 0),
    ADD COLUMN cache_write  integer CHECK (cache_write >= 0),
    ADD COLUMN answer_chars integer CHECK (answer_chars >= 0);
"""
