"""Migration 0003: when each sealed credential expires, and what it may do (F03 slice 2).

Metadata only, never secret: an expiry time (so Yaqzan warns before a key
dies instead of failing mysteriously) and the key's access level as a fixed
choice. There is deliberately no free-text column: a pasted key must have
nowhere to land in plaintext (threat CR-1).

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "key_expiry"

SQL = r"""
ALTER TABLE yaqzan.sealed_secrets
    ADD COLUMN expires_at timestamptz,
    ADD COLUMN access text NOT NULL DEFAULT 'unknown'
        CHECK (access IN ('read_only', 'read_write', 'unknown'));
"""
