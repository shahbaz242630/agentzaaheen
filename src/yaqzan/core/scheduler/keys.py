"""The daily key check: every saved key's expiry, and whether its vendor still accepts it.

Reads only plain facts (expiry dates and the last sync error), never a
ciphertext or a secret. Each key that needs attention becomes a
:class:`KeyAlert` and one log line in plain English; sending P1s to the
Chairman's phone arrives with the notify module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from yaqzan.core import metrics
from yaqzan.core.metrics import KeyHealth
from yaqzan.core.sdk import Severity, explain, get_logger
from yaqzan.core.store import AllSealedSecrets, RevenueRows, Scoped

_LEVEL = {Severity.P1: "error", Severity.P2: "warning", Severity.P3: "info"}


@dataclass(frozen=True)
class KeyAlert:
    """One key that needs the Chairman. ``days_left`` is ``None`` when no expiry is recorded."""

    project_id: UUID
    connector: str
    environment: str
    health: KeyHealth
    severity: Severity
    expires_at: datetime | None
    days_left: int | None


async def check_keys(scope: Scoped, *, now: datetime) -> list[KeyAlert]:
    """Judge every saved key of the tenant; return (and log) the ones that need attention.

    A tenant-wide job: it goes through the workspaces one at a time, behind each wall.
    """
    log = get_logger("yaqzan.keys", tenant_id=str(scope.tenant_id))
    alerts: list[KeyAlert] = []
    for cred in await AllSealedSecrets(scope).dates():
        slot = cred.slot
        state = await RevenueRows(scope.within(slot.project_id)).sync_state(slot.connector)
        last_error = None if state is None else state.last_error_code
        health = metrics.key_health(cred.expires_at, now, last_error_code=last_error)
        severity = health.severity
        if severity is None:
            continue
        days = None if cred.expires_at is None else metrics.days_left(cred.expires_at, now)
        expires = cred.expires_at
        alert = KeyAlert(
            project_id=slot.project_id,
            connector=slot.connector,
            environment=slot.environment,
            health=health,
            severity=severity,
            expires_at=expires,
            days_left=days,
        )
        alerts.append(alert)
        said = explain(health.code, vendor=slot.connector)
        getattr(log, _LEVEL[severity])(
            "key_needs_attention",
            severity=severity.value,
            project_id=str(slot.project_id),
            connector=slot.connector,
            environment=slot.environment,
            expires_at=None if expires is None else expires.isoformat(),
            days_left=days,
            reason_code=health.code,
            what=said.what,
            why=said.why,
            fix=said.fix,
        )
    return alerts
