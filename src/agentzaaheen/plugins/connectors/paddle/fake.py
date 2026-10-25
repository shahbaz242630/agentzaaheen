"""An in-memory Paddle source for tests of anything that consumes revenue.

It passes the same contract suite as the real ``PaddleSource``
(``tests/contract/test_revenue_source_contract.py``), so code tested against
it behaves the same against Paddle.
"""

from __future__ import annotations

from datetime import datetime

from agentzaaheen.core.sdk import Payment, Refund, RevenueBatch, SourceError, SourceErrorCode, Subscription
from agentzaaheen.plugins.connectors.paddle.mapping import SOURCE


class FakePaddleSource:
    """Serves fixed shapes. Set ``fail_with`` to make the next fetches fail with that code."""

    def __init__(
        self,
        subscriptions: tuple[Subscription, ...] = (),
        payments: tuple[Payment, ...] = (),
        refunds: tuple[Refund, ...] = (),
    ) -> None:
        self.subscriptions = subscriptions
        self.payments = payments
        self.refunds = refunds
        self.fail_with: SourceErrorCode | None = None
        self.calls: list[datetime | None] = []

    @property
    def source(self) -> str:
        """The connector id."""
        return SOURCE

    async def fetch(self, *, payments_since: datetime | None, now: datetime) -> RevenueBatch:
        """Everything, with payments limited to those at or after ``payments_since``."""
        self.calls.append(payments_since)
        if self.fail_with is not None:
            raise SourceError(self.fail_with, "the fake was told to fail")
        payments = tuple(
            p for p in self.payments if payments_since is None or p.occurred_at >= payments_since
        )
        return RevenueBatch(
            source=SOURCE,
            subscriptions=self.subscriptions,
            payments=payments,
            refunds=self.refunds,
            fetched_at=now,
        )
