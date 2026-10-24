"""The daily key check: warns 14 days ahead, P1 once expired or refused, in plain English."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from yaqzan.core.broker import save_credential
from yaqzan.core.metrics import KeyHealth
from yaqzan.core.scheduler import KeyAlert, check_keys
from yaqzan.core.sdk import Severity
from yaqzan.core.store import Database, RevenueRows
from yaqzan.core.vault import CredentialSlot, Keyring, MasterKey

TENANT = uuid4()
NOW = datetime(2027, 1, 2, 9, 0, tzinfo=UTC)
RING = Keyring([MasterKey.generate("laptop-test")], "laptop-test")
SECRET = "-".join(["fake", "key", "for", "expiry", "tests"])  # built at runtime, never real


async def save(db: Database, project: UUID, expires_at: datetime | None) -> None:
    where = db.scoped(TENANT, project)
    slot = CredentialSlot.model_validate(
        {
            "tenant_id": TENANT,
            "project_id": project,
            "connector": "paddle",
            "name": "api_key",
            "environment": "production",
        }
    )
    await save_credential(where, RING, slot, SecretStr(SECRET), expires_at=expires_at, access="read_only")


def summary(alert: KeyAlert) -> tuple[KeyHealth, Severity, int | None]:
    return alert.health, alert.severity, alert.days_left


def test_each_key_gets_the_right_alert_and_a_plain_english_line(
    test_db: ScratchDb, capsys: pytest.CaptureFixture[str]
) -> None:
    fine, soon, gone, refused, undated = (uuid4() for _ in range(5))

    async def body() -> list[KeyAlert]:
        await add_workspaces(test_db.app_url, TENANT, fine, soon, gone, refused, undated)
        async with open_db(test_db.app_url) as db:
            await save(db, fine, NOW + timedelta(days=60))
            await save(db, soon, NOW + timedelta(days=14))
            await save(db, gone, NOW - timedelta(hours=1))
            await save(db, refused, NOW + timedelta(days=60))
            await save(db, undated, None)
            await RevenueRows(db.scoped(TENANT, refused)).mark_failure(
                "paddle", at=NOW, error_code="net.auth"
            )
            return await check_keys(db.scoped(TENANT), now=NOW)

    alerts = {a.project_id: a for a in run(body())}
    assert fine not in alerts
    assert summary(alerts[soon]) == (KeyHealth.EXPIRING, Severity.P2, 14)
    assert summary(alerts[gone]) == (KeyHealth.EXPIRED, Severity.P1, -1)
    assert summary(alerts[refused]) == (KeyHealth.REFUSED, Severity.P1, 60)
    assert summary(alerts[undated]) == (KeyHealth.NO_DATE, Severity.P3, None)

    logged = capsys.readouterr().err
    parsed = [json.loads(x) for x in logged.splitlines() if "key_needs_attention" in x]
    lines = {line["project_id"]: line for line in parsed}
    assert set(lines) == {str(p) for p in (soon, gone, refused, undated)}
    soon_line = lines[str(soon)]
    assert (soon_line["level"], soon_line["severity"], soon_line["days_left"]) == ("warning", "P2", 14)
    assert soon_line["what"] == "The Paddle API key expires within 14 days."
    assert soon_line["fix"].startswith("Create a new read-only key in Paddle's settings")
    assert lines[str(gone)]["level"] == "error"
    assert lines[str(undated)]["level"] == "info"
    assert SECRET not in logged


def test_a_tenant_with_no_keys_has_nothing_to_say(test_db: ScratchDb) -> None:
    async def body() -> list[KeyAlert]:
        async with open_db(test_db.app_url) as db:
            return await check_keys(db.scoped(uuid4()), now=NOW)

    assert run(body()) == []
