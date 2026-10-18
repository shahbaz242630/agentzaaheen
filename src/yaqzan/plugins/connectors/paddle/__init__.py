"""Paddle connector: read-only subscriptions, payments and refunds (METRICS §3: Zaaheen, Eva).

What it does:
    ``PaddleSource`` lists subscriptions, transactions, adjustments and the
    discounts those subscriptions use, through the ``JsonGetter`` the core
    hands it, and maps them to the SDK's ``Subscription``, ``Payment`` and
    ``Refund``. ``FakePaddleSource`` serves fixed shapes for tests.

Public interface:
    ``PaddleSource``, ``FakePaddleSource``, ``LIVE_BASE_URL``,
    ``SANDBOX_BASE_URL``, ``ALLOWED_HOSTS``, ``API_VERSION_HEADER``,
    ``SOURCE``. Manifest: ``plugin.yaml`` next to this file.

How to test:
    ``uv run pytest tests/unit/test_paddle_mapping.py tests/contract``
    (synthetic, scrubbed fixtures; no real network).

Must never:
    Call anything but the four GET list endpoints; read names, emails,
    addresses or custom data; hold or log the API key (the core's client
    carries it); guess an amount it cannot work out exactly.
"""

from yaqzan.plugins.connectors.paddle.adapter import (
    ALLOWED_HOSTS,
    API_VERSION_HEADER,
    LIVE_BASE_URL,
    SANDBOX_BASE_URL,
    PaddleSource,
)
from yaqzan.plugins.connectors.paddle.fake import FakePaddleSource
from yaqzan.plugins.connectors.paddle.mapping import SOURCE

__all__ = [
    "ALLOWED_HOSTS",
    "API_VERSION_HEADER",
    "LIVE_BASE_URL",
    "SANDBOX_BASE_URL",
    "SOURCE",
    "FakePaddleSource",
    "PaddleSource",
]
