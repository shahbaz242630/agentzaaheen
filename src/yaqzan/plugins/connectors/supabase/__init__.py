"""Supabase connector: each backend project's state, service health, disk use and API traffic (F03 slice 6).

What it does:
    ``SupabaseSource`` reads one project through four named GETs (project,
    health, disk utilisation, the last day's API counts) via the ``JsonReader``
    the core hands it, and maps them to ``BackendHealth``: codes and numbers
    only. ``FakeSupabaseApi`` stands in for the API in tests.

Public interface:
    ``SupabaseSource``, ``FakeSupabaseApi``, ``BASE_URL``, ``ALLOWED_HOSTS``,
    ``API_HEADERS``, ``SERVICES``, ``SOURCE``. Manifest: ``plugin.yaml``.

How to test:
    ``uv run pytest tests/unit/test_supabase.py`` (a pretend Supabase; no real network).

Must never:
    Call anything but its four named GETs; read a free-text field (error
    messages, descriptions, names); run SQL or read table data (the token is
    never given the Database permission); hold or log the token.
"""

from yaqzan.plugins.connectors.supabase.adapter import ALLOWED_HOSTS, API_HEADERS, BASE_URL, SupabaseSource
from yaqzan.plugins.connectors.supabase.fake import FakeSupabaseApi
from yaqzan.plugins.connectors.supabase.mapping import SERVICES, SOURCE

__all__ = [
    "ALLOWED_HOSTS",
    "API_HEADERS",
    "BASE_URL",
    "SERVICES",
    "SOURCE",
    "FakeSupabaseApi",
    "SupabaseSource",
]
