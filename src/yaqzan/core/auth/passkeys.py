"""Passkeys (WebAuthn) for approving: set one up, and check a signed answer (ADR-SEC-007).

All the cryptography is done by the ``webauthn`` library (Duo Labs, BSD-3):
signature, origin, site name (RP id), challenge, user verification and the
signature counter. This module only fixes the settings and keeps the
challenges: random, bound to one purpose (``enrol`` or ``approve:<id>``),
used once, valid for two minutes, in memory only.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

CHALLENGE_TTL = timedelta(minutes=2)
MAX_ANSWER_BYTES = 16_384
MAX_OPEN_CHALLENGES = 32
USER_ID = b"yaqzan-chairman"  # one approver per tenant; the same id for every passkey
# "step:..." names one exact change (an agent on, a cap of exactly N): a signature for it fits nothing else.
_PURPOSE = r"^(enrol|approve:[0-9a-f-]{36}|step:[a-z0-9:._-]{1,160})$"


class PasskeyError(Exception):
    """A passkey step failed. ``code`` says why (safe to log, audit and show)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Site(BaseModel):
    """Where passkeys work: WebAuthn needs a name, not an IP, so the dashboard is opened at localhost."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rp_id: str = Field(default="localhost", pattern=r"^[a-z0-9.-]{1,253}$")
    rp_name: str = "Yaqzan"
    origin: str = Field(pattern=r"^https?://[a-z0-9.-]+(:[0-9]{1,5})?$")


@dataclass(frozen=True)
class NewPasskey:
    """What a successful set-up gives: public data only."""

    credential_id: bytes
    public_key: bytes
    sign_count: int


@dataclass(frozen=True)
class KnownPasskey:
    """A stored passkey, as the check needs it."""

    credential_id: bytes
    public_key: bytes
    sign_count: int


class Challenges:
    """Open challenges, one per purpose: each is used once and lapses after ``CHALLENGE_TTL``."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._open: dict[str, tuple[bytes, datetime]] = {}

    def issue(self, purpose: str) -> bytes:
        """A fresh challenge for ``purpose``; any earlier one for it is dropped."""
        if not _valid_purpose(purpose):
            raise ValueError("unknown challenge purpose")
        if len(self._open) >= MAX_OPEN_CHALLENGES and purpose not in self._open:
            oldest = min(self._open, key=lambda p: self._open[p][1])
            del self._open[oldest]
        challenge = secrets.token_bytes(32)
        self._open[purpose] = (challenge, self._clock() + CHALLENGE_TTL)
        return challenge

    def take(self, purpose: str) -> bytes:
        """The open challenge for ``purpose``, removed so it can never be used again."""
        found = self._open.pop(purpose, None)
        if found is None:
            raise PasskeyError("passkey.no_challenge")
        challenge, expires = found
        if self._clock() > expires:
            raise PasskeyError("passkey.challenge_expired")
        return challenge


def _valid_purpose(purpose: str) -> bool:
    return re.fullmatch(_PURPOSE, purpose) is not None


def _answer(answer: dict[str, object]) -> str:
    text = json.dumps(answer, separators=(",", ":"))
    if len(text.encode()) > MAX_ANSWER_BYTES:
        raise PasskeyError("passkey.answer_too_big")
    return text


def enrol_options(site: Site, challenge: bytes, existing: list[bytes]) -> dict[str, object]:
    """What the browser needs to make a passkey on this laptop (Windows Hello), user verification required."""
    options = generate_registration_options(
        rp_id=site.rp_id,
        rp_name=site.rp_name,
        user_id=USER_ID,
        user_name="chairman",
        user_display_name="Yaqzan Chairman",
        challenge=challenge,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=c) for c in existing],
    )
    loaded: dict[str, object] = json.loads(options_to_json(options))
    return loaded


def verify_enrol(site: Site, challenge: bytes, answer: dict[str, object]) -> NewPasskey:
    """Check the browser's answer to :func:`enrol_options`; the new passkey's public data."""
    try:
        verified = verify_registration_response(
            credential=_answer(answer),
            expected_challenge=challenge,
            expected_rp_id=site.rp_id,
            expected_origin=site.origin,
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError, KeyError, TypeError):
        raise PasskeyError("passkey.not_verified") from None
    return NewPasskey(
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
    )


def approve_options(site: Site, challenge: bytes, known: list[KnownPasskey]) -> dict[str, object]:
    """What the browser needs to ask Windows Hello to sign this one challenge."""
    options = generate_authentication_options(
        rp_id=site.rp_id,
        challenge=challenge,
        allow_credentials=[PublicKeyCredentialDescriptor(id=k.credential_id) for k in known],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    loaded: dict[str, object] = json.loads(options_to_json(options))
    return loaded


def verify_approval(
    site: Site, challenge: bytes, answer: dict[str, object], known: list[KnownPasskey]
) -> tuple[KnownPasskey, int]:
    """Check a signed answer against the stored passkey it names; that passkey and its new counter."""
    text = _answer(answer)
    raw_id = answer.get("rawId", answer.get("id"))
    match = next((k for k in known if isinstance(raw_id, str) and _b64url(k.credential_id) == raw_id), None)
    if match is None:
        raise PasskeyError("passkey.unknown")
    try:
        verified = verify_authentication_response(
            credential=text,
            expected_challenge=challenge,
            expected_rp_id=site.rp_id,
            expected_origin=site.origin,
            credential_public_key=match.public_key,
            credential_current_sign_count=match.sign_count,
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError, KeyError, TypeError):
        raise PasskeyError("passkey.not_verified") from None
    return match, verified.new_sign_count


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
