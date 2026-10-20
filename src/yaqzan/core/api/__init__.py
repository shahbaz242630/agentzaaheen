"""HTTP and MCP endpoint for the dashboard, desktop app and Claude (read-only role).

What it does (so far):
    Serves the dashboard (F04): a read-only JSON API (projects, a project's
    cards, the chat's model list) and the built web UI, with a strict
    Content-Security-Policy and a host allowlist. Sample data
    (:class:`SamplePortfolio`) with real cards laid over it
    (:class:`~yaqzan.core.api.overlay.OverlayPortfolio`). The chat (F05)
    answers from the cards, written out as text by ``facts``. The Customize
    page (F06) is sent the registry of connectors, skills and plugins, with
    where each connector is in use and whether each is on (``registry``);
    with a database it can switch an item off, ask for one to be switched on
    and withdraw that request (ADR-030).

Public interface:
    ``create_app``, ``Portfolio``, ``ChatBackend``, ``RegistrySwitches``, ``SamplePortfolio``,
    ``LOCAL_HOSTS`` and
    the view shapes in ``views`` and ``registry``.

How to test:
    ``uv run pytest tests/unit/test_api_views.py tests/unit/test_api_app.py``
    ``uv run pytest tests/unit/test_api_registry.py``
    See it: ``uv run python -m tools.dashboard`` (after building the UI).

Must never:
    Contain business logic (numbers come from metrics); offer a POST that can
    widen what a model may use (only off, ask and withdraw exist until
    approvals arrive with passkey step-up); approve actions for the
    ai-operator role; listen beyond the laptop without an auth layer in front.
"""

from yaqzan.core.api.app import LOCAL_HOSTS, ChatBackend, Portfolio, RegistrySwitches, create_app
from yaqzan.core.api.sample import SamplePortfolio

__all__ = ["LOCAL_HOSTS", "ChatBackend", "Portfolio", "RegistrySwitches", "SamplePortfolio", "create_app"]
