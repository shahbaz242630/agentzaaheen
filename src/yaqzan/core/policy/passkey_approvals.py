"""Setting up the Chairman's passkey, and approving registry requests with it (ADR-SEC-007).

Set-up works only while the dashboard was started with a one-time code (it
must be sent back); five wrong codes close it until the next start. If a
passkey already exists, set-up *replaces* it (the old one is revoked and
stops working): that is how a lost passkey is recovered, by someone at this
laptop's terminal (Chairman, 20 Oct 2026). Approving: a challenge is issued for one request only, the
browser has Windows Hello sign it, and the signed answer is checked here with
the stored public key. Only then is ``RegistryControl.approve`` called, with
the time of that check as the step-up. Every set-up, approval and failed check
is audited (never the credential, the signature or the challenge).
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from yaqzan.core.auth import (
    Challenges,
    KnownPasskey,
    PasskeyError,
    Site,
    approve_options,
    enrol_options,
    verify_approval,
    verify_enrol,
)
from yaqzan.core.policy.access import Channel, Role
from yaqzan.core.policy.audit import AuditDraft, AuditOutcome
from yaqzan.core.policy.ledger import Ledger
from yaqzan.core.policy.registry_control import RegistryControl, Verdict
from yaqzan.core.sdk import get_logger, say
from yaqzan.core.store import Database, PasskeyRow, PasskeyRows

MAX_WRONG_CODES = 5
# The dashboard on this laptop acts as the Chairman (no login yet; it serves localhost only).
CHAIRMAN = "user:chairman"
ENROL = "enrol"


class PasskeyApprovals:
    """One tenant's passkey: set it up once, then use it for each approval. Acts as the Chairman."""

    def __init__(  # noqa: PLR0913  # the database, tenant, switches, site, and how set-up and time work
        self,
        db: Database,
        tenant_id: UUID,
        control: RegistryControl,
        site: Site,
        *,
        enrol_code: str | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._rows = PasskeyRows(db.scoped(tenant_id))
        self._ledger = Ledger(db, tenant_id)
        self._tenant = tenant_id
        self._control = control
        self._site = site
        self._code = enrol_code
        self._wrong = 0
        self._clock = clock
        self._challenges = Challenges(clock)

    async def _known(self) -> list[KnownPasskey]:
        row = await self._rows.active()
        return [] if row is None else [KnownPasskey(row.credential_id, row.public_key, row.sign_count)]

    async def status(self) -> tuple[bool, bool]:
        """(a passkey exists, set-up or replacement is open on this run)."""
        enrolled = await self._rows.active() is not None
        return enrolled, self._code is not None and self._wrong < MAX_WRONG_CODES

    def _check_code(self, code: str) -> None:
        if self._code is None or self._wrong >= MAX_WRONG_CODES:
            raise PasskeyError("passkey.enrol_closed")
        if not hmac.compare_digest(code.encode(), self._code.encode()):
            self._wrong += 1
            raise PasskeyError("passkey.wrong_code")

    async def enrol_options(self, code: str) -> dict[str, object]:
        """Options to make the passkey, if set-up is open and ``code`` is the one printed at start.

        The current passkey is not excluded: replacing it on the same laptop must work.
        """
        self._check_code(code)
        return enrol_options(self._site, self._challenges.issue(ENROL), [])

    async def enrol(self, code: str, answer: dict[str, object]) -> None:
        """Check the new passkey and keep its public half, replacing any old one; audited either way."""
        now = self._clock()
        replacing = False
        try:
            self._check_code(code)
            made = verify_enrol(self._site, self._challenges.take(ENROL), answer)
            row = PasskeyRow(made.credential_id, made.public_key, made.sign_count, CHAIRMAN, now)
            replacing = await self._rows.active() is not None
            done = await (self._rows.replace if replacing else self._rows.add_first)(row)
            if not done:
                raise PasskeyError("passkey.not_saved")
        except PasskeyError as exc:
            await self._audit("passkey.enrol", exc.code, now, allowed=False)
            raise
        self._code = None  # one set-up per printed code
        await self._audit(
            "passkey.enrol", "passkey.replaced" if replacing else "passkey.enrolled", now, allowed=True
        )

    async def approve_options(self, request_id: UUID) -> dict[str, object]:
        """Options for Windows Hello to sign a challenge for this one request."""
        verdict = await self._control.approvable(request_id)
        if verdict is not Verdict.DONE:
            raise PasskeyError(verdict.value)
        known = await self._known()
        if not known:
            raise PasskeyError(Verdict.NO_PASSKEY.value)
        return approve_options(self._site, self._challenges.issue(f"approve:{request_id}"), known)

    async def approve(self, request_id: UUID, answer: dict[str, object]) -> Verdict:
        """Check the signed answer for this request; if it holds, approve with it as the step-up."""
        now = self._clock()
        try:
            challenge = self._challenges.take(f"approve:{request_id}")
            used, count = verify_approval(self._site, challenge, answer, await self._known())
            if not await self._rows.record_use(used.credential_id, count, now):
                raise PasskeyError("passkey.counter_went_back")
        except PasskeyError as exc:
            await self._audit("registry.approve", exc.code, now, allowed=False, request_id=request_id)
            return Verdict.PASSKEY_FAILED
        return await self._control.approve(
            request_id, role=Role.CHAIRMAN, channel=Channel.DASHBOARD, step_up_at=now, now=now
        )

    async def step_up_options(self, purpose: str) -> dict[str, object]:
        """Options for Windows Hello to sign one exact change, named by ``purpose`` (``step:...``)."""
        if not purpose.startswith("step:"):
            raise PasskeyError("passkey.bad_purpose")
        known = await self._known()
        if not known:
            raise PasskeyError(Verdict.NO_PASSKEY.value)
        return approve_options(self._site, self._challenges.issue(purpose), known)

    async def step_up(self, purpose: str, answer: dict[str, object]) -> datetime | None:
        """The time of a good signature for exactly ``purpose``, or ``None`` (audited) if it does not hold."""
        now = self._clock()
        try:
            challenge = self._challenges.take(purpose)
            used, count = verify_approval(self._site, challenge, answer, await self._known())
            if not await self._rows.record_use(used.credential_id, count, now):
                raise PasskeyError("passkey.counter_went_back")
        except PasskeyError as exc:
            await self._audit("passkey.step_up", exc.code, now, allowed=False, purpose=purpose)
            return None
        await self._audit("passkey.step_up", "passkey.step_up_ok", now, allowed=True, purpose=purpose)
        return now

    async def _audit(  # noqa: PLR0913  # what, why, when, the outcome, and what it was for
        self,
        action: str,
        reason: str,
        now: datetime,
        *,
        allowed: bool,
        request_id: UUID | None = None,
        purpose: str | None = None,
    ) -> None:
        say(get_logger("yaqzan.passkey"), "passkey_decided", action=action, verdict=reason)
        await self._ledger.record(
            AuditDraft(
                at=now,
                actor=CHAIRMAN,
                action=action,
                outcome=AuditOutcome.ALLOW if allowed else AuditOutcome.DENY,
                reason_code=reason,
                correlation_id=uuid4(),
                target_ref=purpose if request_id is None else f"request:{request_id}",
                details={} if request_id is None else {"request": str(request_id)},
            )
        )
