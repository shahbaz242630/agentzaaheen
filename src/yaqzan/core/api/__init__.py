"""HTTP and MCP endpoint for the dashboard, desktop app and Claude (read-only role).

What it does (so far):
    Serves the dashboard (F04): a read-only JSON API (projects, a project's
    cards, the chat's model list) and the built web UI, with a strict
    Content-Security-Policy and a host allowlist. Sample data
    (:class:`SamplePortfolio`) with real cards laid over it
    (:class:`~yaqzan.core.api.overlay.OverlayPortfolio`). The chat (F05)
    answers from the cards, written out as text by ``facts``.

Public interface:
    ``create_app``, ``Portfolio``, ``ChatBackend``, ``SamplePortfolio``, ``LOCAL_HOSTS`` and
    the view shapes in ``views``.

How to test:
    ``uv run pytest tests/unit/test_api_views.py tests/unit/test_api_app.py``
    See it: ``uv run python -m tools.dashboard`` (after building the UI).

Must never:
    Contain business logic (numbers come from metrics); offer anything but GET
    (the chat POST, which changes nothing, aside) until approvals arrive with
    passkey step-up; approve actions for the
    ai-operator role; listen beyond the laptop without an auth layer in front.
"""

from yaqzan.core.api.app import LOCAL_HOSTS, ChatBackend, Portfolio, create_app
from yaqzan.core.api.sample import SamplePortfolio

__all__ = ["LOCAL_HOSTS", "ChatBackend", "Portfolio", "SamplePortfolio", "create_app"]
