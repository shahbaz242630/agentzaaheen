"""Availability connector: each project's external checks, counted by the hour (ADR-032, SLO slice 1).

What it does:
    ``AvailabilitySource`` reads the checker's hourly counts (checks, and how
    many found the project up) summed per project, with one fixed SQL query
    through the ``JsonQuerier`` the core hands it (``UptimeHistory``).
    ``FakeD1`` stands in for Cloudflare in tests.

Public interface:
    ``AvailabilitySource``, ``FakeD1``, ``d1_answer``, ``query_path``,
    ``BASE_URL``, ``ALLOWED_HOSTS``, ``API_HEADERS``, ``KEY_HEADER``,
    ``HOURS_QUERY``, ``READ_QUERIES``, ``SOURCE``. Manifest: ``plugin.yaml``.

How to test:
    ``uv run pytest tests/unit/test_availability.py`` (a pretend D1; no real network).

Must never:
    Send any SQL but ``HOURS_QUERY`` (the key can only read, too); read an
    address (the checker keeps only a hash); hold or log the key.
"""

from agentzaaheen.plugins.connectors.availability.adapter import (
    ALLOWED_HOSTS,
    API_HEADERS,
    BASE_URL,
    HOURS_QUERY,
    KEY_HEADER,
    READ_QUERIES,
    SOURCE,
    AvailabilitySource,
    query_path,
)
from agentzaaheen.plugins.connectors.availability.fake import FakeD1, d1_answer

__all__ = [
    "ALLOWED_HOSTS",
    "API_HEADERS",
    "BASE_URL",
    "HOURS_QUERY",
    "KEY_HEADER",
    "READ_QUERIES",
    "SOURCE",
    "AvailabilitySource",
    "FakeD1",
    "d1_answer",
    "query_path",
]
