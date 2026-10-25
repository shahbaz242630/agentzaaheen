"""Proving it is the Chairman: passkeys (WebAuthn, Windows Hello) for approvals (ADR-SEC-007).

What it does:
    Makes the options a browser needs to set up a passkey or sign one
    challenge, and checks the answers with the ``webauthn`` library
    (signature, origin, site name, user verification, counter). Keeps the
    open challenges: one per purpose, used once, two minutes.

Public interface:
    ``Site``, ``Challenges``, ``enrol_options``, ``verify_enrol``,
    ``approve_options``, ``verify_approval``, ``NewPasskey``, ``KnownPasskey``,
    ``PasskeyError``, ``CHALLENGE_TTL``.

How to test:
    ``uv run pytest tests/unit/test_auth_passkeys.py`` (a software authenticator; no browser).

Must never:
    Accept an answer without user verification, for another origin or site
    name, for a challenge that was used, expired or issued for another
    purpose; parse WebAuthn data itself; store a private key; touch the
    database, the network or the vault.
"""

from agentzaaheen.core.auth.passkeys import (
    CHALLENGE_TTL,
    Challenges,
    KnownPasskey,
    NewPasskey,
    PasskeyError,
    Site,
    approve_options,
    enrol_options,
    verify_approval,
    verify_enrol,
)

__all__ = [
    "CHALLENGE_TTL",
    "Challenges",
    "KnownPasskey",
    "NewPasskey",
    "PasskeyError",
    "Site",
    "approve_options",
    "enrol_options",
    "verify_approval",
    "verify_enrol",
]
