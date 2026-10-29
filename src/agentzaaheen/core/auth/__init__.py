"""Proving it is the Chairman: passkeys (WebAuthn, Windows Hello) for approvals and sign-in.

ADR-SEC-007 (approvals) and ADR-SEC-010 (sign-in sessions).

What it does:
    Makes the options a browser needs to set up a passkey or sign one
    challenge, and checks the answers with the ``webauthn`` library
    (signature, origin, site name, user verification, counter). Keeps the
    open challenges: one per purpose, used once, two minutes. Keeps who is
    signed in (``Sessions``: token hashes in memory, 3 h idle, 12 h at most)
    and the wait after failed sign-ins (``SignInWait``).

Public interface:
    ``Site``, ``Challenges``, ``enrol_options``, ``verify_enrol``,
    ``approve_options``, ``verify_approval``, ``NewPasskey``, ``KnownPasskey``,
    ``PasskeyError``, ``CHALLENGE_TTL``, ``SIGN_IN``; ``Sessions``, ``SessionCheck``,
    ``Started``, ``SignInWait``, ``IDLE_AFTER``, ``LONGEST``, ``MAX_LIVE``.

How to test:
    ``uv run pytest tests/unit/test_auth_passkeys.py tests/unit/test_auth_sessions.py``
    (a software authenticator; no browser).

Must never:
    Accept an answer without user verification, for another origin or site
    name, for a challenge that was used, expired or issued for another
    purpose; parse WebAuthn data itself; store a private key; touch the
    database, the network or the vault; keep a session token itself (only its hash).
"""

from agentzaaheen.core.auth.passkeys import (
    CHALLENGE_TTL,
    SIGN_IN,
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
from agentzaaheen.core.auth.sessions import (
    IDLE_AFTER,
    LONGEST,
    MAX_LIVE,
    SessionCheck,
    Sessions,
    SignInWait,
    Started,
)

__all__ = [
    "CHALLENGE_TTL",
    "IDLE_AFTER",
    "LONGEST",
    "MAX_LIVE",
    "SIGN_IN",
    "Challenges",
    "KnownPasskey",
    "NewPasskey",
    "PasskeyError",
    "SessionCheck",
    "Sessions",
    "SignInWait",
    "Site",
    "Started",
    "approve_options",
    "enrol_options",
    "verify_approval",
    "verify_enrol",
]
