"""Healthchecks connector: each check's slug, status and last ping (F11 slice 5a).

What it does:
    ``HealthchecksSource`` reads every check of one Healthchecks project with
    one GET through the ``JsonGetter`` the core hands it, and keeps only each
    check's slug, status and last ping (``HeartbeatReport``).
    ``FakeHealthchecksApi`` stands in for the API in tests.

Public interface:
    ``HealthchecksSource``, ``FakeHealthchecksApi``, ``BASE_URL``,
    ``ALLOWED_HOSTS``, ``API_HEADERS``, ``CHECKS_PATH``, ``KEY_HEADER``,
    ``SOURCE``. Manifest: ``plugin.yaml``.

How to test:
    ``uv run pytest tests/unit/test_healthchecks.py`` (a pretend Healthchecks; no real network).

Must never:
    Ping, pause or change a check (the key is read-only); read a check's
    name, notes or ping address; hold or log the key.
"""

from agentzaaheen.plugins.connectors.healthchecks.adapter import (
    ALLOWED_HOSTS,
    API_HEADERS,
    BASE_URL,
    CHECKS_PATH,
    KEY_HEADER,
    SOURCE,
    HealthchecksSource,
)
from agentzaaheen.plugins.connectors.healthchecks.fake import FakeHealthchecksApi

__all__ = [
    "ALLOWED_HOSTS",
    "API_HEADERS",
    "BASE_URL",
    "CHECKS_PATH",
    "KEY_HEADER",
    "SOURCE",
    "FakeHealthchecksApi",
    "HealthchecksSource",
]
