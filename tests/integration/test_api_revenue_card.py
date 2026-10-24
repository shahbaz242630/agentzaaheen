"""Stored Paddle rows reach the dashboard card unchanged, and stale data becomes "unknown"."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.fixtures import paddle_world as world
from tests.integration.conftest import ScratchDb, add_workspaces, open_db, run
from tests.integration.test_scheduler_collect import END, PROJECT, SCHEDULE, START, TENANT, fake
from yaqzan.core.api.revenue import revenue_card
from yaqzan.core.api.views import Card, Count, Money, NotKnown
from yaqzan.core.metrics import Freshness
from yaqzan.core.scheduler import revenue_snapshot, sync_revenue


@pytest.fixture(autouse=True)
def _workspace(test_db: ScratchDb) -> None:
    run(add_workspaces(test_db.app_url, TENANT, PROJECT))


def _card_at(test_db: ScratchDb, looked_at: datetime) -> Card:
    async def body() -> Card:
        async with open_db(test_db.app_url) as db:
            await sync_revenue(db.scoped(TENANT, PROJECT), fake(), now=world.NOW)
            snap = await revenue_snapshot(
                db.scoped(TENANT, PROJECT), "paddle", now=looked_at, schedule=SCHEDULE, start=START, end=END
            )
            return revenue_card(
                snap,
                card_id="paddle",
                title="Revenue",
                source_label="Paddle",
                home_currency="USD",
                period_label="This month",
            )

    return run(body())


def test_a_fresh_sync_shows_the_stored_numbers_per_currency(test_db: ScratchDb) -> None:
    card = _card_at(test_db, world.NOW + timedelta(minutes=5))
    values = {m.label: m.value for m in card.metrics}
    assert card.freshness is Freshness.FRESH
    assert card.sample is False
    assert values["MRR (USD)"] == Money(minor=1300, currency="USD")
    assert values["MRR (GBP)"] == Money(minor=300, currency="GBP")
    assert values["Active subscribers"] == Count(value=3)
    assert values["Trials"] == Count(value=1)
    assert values["Failed payments"] == Count(value=2)
    assert values["Refunds"] == Count(value=2)
    assert values["Revenue excl. tax (USD)"] == Money(minor=500, currency="USD")
    assert values["Revenue excl. tax (GBP)"] == Money(minor=300, currency="GBP")


def test_old_data_shows_unknown_never_the_old_numbers(test_db: ScratchDb) -> None:
    card = _card_at(test_db, world.NOW + SCHEDULE * 3)
    assert card.freshness is Freshness.STALE
    assert card.last_success_at == world.NOW
    assert all(m.value == NotKnown(reason="freshness.stale") for m in card.metrics)
