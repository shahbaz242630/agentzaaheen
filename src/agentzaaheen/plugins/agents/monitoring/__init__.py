"""The Monitoring agent (role pack ``hr/roles/monitoring``): code only, read only (F11 slice 4).

Public interface:
    :func:`watch` judges one project's newest uptime check against its
    bookmark and says what to write down; :data:`BOOKMARK` names that bookmark.

Must never:
    Reach the network, send a phone alert, or hold a key. It is given the
    uptime watch's newest check; the runner saves what it writes.
"""

from agentzaaheen.plugins.agents.monitoring.uptime import (
    BOOKMARK,
    FRESH,
    UNLISTED,
    UNREAD,
    UP,
    Note,
    Watched,
    watch,
)

__all__ = ["BOOKMARK", "FRESH", "UNLISTED", "UNREAD", "UP", "Note", "Watched", "watch"]
