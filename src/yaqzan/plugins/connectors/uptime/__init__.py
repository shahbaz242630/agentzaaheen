"""Uptime connector: whether each of a project's public addresses answers as it should (F03 slice 4).

What it does:
    ``UptimeSource`` checks each address on a project's list once, through
    the ``StatusGetter`` the core hands it (status code and timing only), and
    judges it by its rule (``ok``: a page or a redirect; ``answer``: anything
    but a server error). An address that does not answer counts as down and
    never stops the others. ``FakeStatusGetter`` stands in for the network.

Public interface:
    ``UptimeSource``, ``FakeStatusGetter``, ``SOURCE``. Manifest: ``plugin.yaml``
    next to this file. The address list itself is local to the laptop and
    never committed (Rulebook §7b).

How to test:
    ``uv run pytest tests/unit/test_uptime.py`` (a pretend network; no real requests).

Must never:
    Read a page (only its status line reaches it); follow a redirect; check
    an address that is not on the list; hold a credential (it needs none).
"""

from yaqzan.plugins.connectors.uptime.adapter import SOURCE, UptimeSource
from yaqzan.plugins.connectors.uptime.fake import FakeStatusGetter

__all__ = ["SOURCE", "FakeStatusGetter", "UptimeSource"]
