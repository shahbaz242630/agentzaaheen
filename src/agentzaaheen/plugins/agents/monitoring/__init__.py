"""The Monitoring agent (role pack ``hr/roles/monitoring``): code only, read only (F11 slices 4-5).

Public interface:
    :func:`watch` judges one project's newest uptime check against its
    bookmark :data:`BOOKMARK`; :func:`watch_heartbeats` judges its
    Healthchecks checks against :data:`HEARTBEATS_BOOKMARK`. Each says what to
    write down and the new bookmark.

Must never:
    Reach the network, send a phone alert, or hold a key. It is given the
    watches' newest reads; the runner saves what it writes.
"""

from agentzaaheen.plugins.agents.monitoring.heartbeats import BOOKMARK as HEARTBEATS_BOOKMARK
from agentzaaheen.plugins.agents.monitoring.heartbeats import watch_heartbeats
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

__all__ = [
    "BOOKMARK",
    "FRESH",
    "HEARTBEATS_BOOKMARK",
    "UNLISTED",
    "UNREAD",
    "UP",
    "Note",
    "Watched",
    "watch",
    "watch_heartbeats",
]
