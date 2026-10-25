"""Migration 0005: the Chairman's passkey, for approving (F06 slice 2c, ADR-SEC-007).

Public data only: the credential id, its public key and its signature
counter. The private key never leaves the laptop's authenticator (Windows
Hello). At most one passkey is active per tenant (the database enforces it);
a lost one is replaced by revoking it and adding the new one, so the record
stays. The app may add a passkey, move its counter and last-use time forward,
and revoke it, nothing else: it cannot swap a public key or delete a row.

Never edit this file once merged: fix forward with a new migration.
"""

NAME = "passkeys"

SQL = r"""
CREATE TABLE agentzaaheen.passkeys (
    tenant_id     uuid NOT NULL,
    credential_id bytea NOT NULL CHECK (octet_length(credential_id) BETWEEN 16 AND 1023),
    public_key    bytea NOT NULL CHECK (octet_length(public_key) BETWEEN 32 AND 2048),
    sign_count    bigint NOT NULL CHECK (sign_count >= 0),
    created_by    text NOT NULL CHECK (created_by ~ '^user:[A-Za-z0-9_.-]{1,64}$'),
    created_at    timestamptz NOT NULL,
    last_used_at  timestamptz,
    revoked_at    timestamptz,
    PRIMARY KEY (tenant_id, credential_id)
);
-- One active passkey per tenant.
CREATE UNIQUE INDEX passkeys_one_active ON agentzaaheen.passkeys (tenant_id) WHERE revoked_at IS NULL;

-- A revoked passkey stays revoked.
CREATE FUNCTION agentzaaheen.passkey_revoked_stays() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF OLD.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'a revoked passkey cannot change' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER passkey_revoked_check BEFORE UPDATE ON agentzaaheen.passkeys
    FOR EACH ROW EXECUTE FUNCTION agentzaaheen.passkey_revoked_stays();

REVOKE ALL ON agentzaaheen.passkeys FROM PUBLIC;
REVOKE ALL ON FUNCTION agentzaaheen.passkey_revoked_stays() FROM PUBLIC;
GRANT SELECT, INSERT ON agentzaaheen.passkeys TO agentzaaheen_app;
GRANT UPDATE (sign_count, last_used_at, revoked_at) ON agentzaaheen.passkeys TO agentzaaheen_app;
"""
