"""HTTP and MCP endpoint for the dashboard, desktop app and Claude (read-only role).

What it does (so far):
    Serves the dashboard (F04): a read-only JSON API (projects, a project's
    cards, the chat's model list) and the built web UI, with a strict
    Content-Security-Policy and a host allowlist. Slice 1 uses sample data
    (:class:`SamplePortfolio`); real numbers replace it in slice 2.

Public interface:
    ``create_app``, ``Portfolio``, ``SamplePortfolio``, ``LOCAL_HOSTS`` and
    the view shapes in ``views``.

How to test:
    ``uv run pytest tests/unit/test_api_views.py tests/unit/test_api_app.py``
    See it: ``uv run python -m tools.dashboard`` (after building the UI).

Must never:
    Contain business logic (numbers come from metrics); offer anything but GET
    until approvals arrive with passkey step-up; approve actions for the
    ai-operator role; listen beyond the laptop without an auth layer in front.
"""

from yaqzan.core.api.app import LOCAL_HOSTS, Portfolio, create_app
from yaqzan.core.api.sample import SamplePortfolio

__all__ = ["LOCAL_HOSTS", "Portfolio", "SamplePortfolio", "create_app"]
