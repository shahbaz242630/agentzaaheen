"""Delivers alerts to the Chairman (ADR-031: ntfy now; Telegram and email later).

What it does (so far):
    :class:`UptimeWatch` decides, round by round, when an uptime change is
    worth a phone alert (two failed checks in a row, one "back up" message,
    no repeats) and words it from a fixed template. :func:`deliver` sends the
    alerts through the core's ``PushClient``; a failed send is logged, never
    raised, so an alert problem can never stop the dashboard.

Public interface:
    ``UptimeWatch``, ``Alert``, ``REASON_WORDS``, ``DEFAULT_THRESHOLD``, ``deliver``.

How to test:
    ``uv run pytest tests/unit/test_notify_uptime.py`` (no real network).

Must never:
    Accept approvals or any command (a low-assurance channel can only tell,
    SECURITY AU-2); put text a website, log or person wrote into an alert;
    send a full address; talk HTTP itself (only through ``core.net``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from yaqzan.core.notify.uptime import DEFAULT_THRESHOLD, REASON_WORDS, Alert, UptimeWatch
from yaqzan.core.sdk import SourceError, get_logger, say


class Pusher(Protocol):
    """Sends one alert (``core.net.PushClient`` in real use)."""

    async def send(self, title: str, message: str, *, priority: str = "default") -> None:
        """Send it, or raise ``SourceError``."""
        ...


async def deliver(alerts: Sequence[Alert], pusher: Pusher) -> int:
    """Send each alert; return how many went out. Failures are logged (never the text), not raised."""
    log = get_logger("yaqzan.notify")
    sent = 0
    for alert in alerts:
        try:
            await pusher.send(alert.title, alert.message, priority=alert.priority)
        except SourceError as exc:
            say(log, "alert_not_sent", project=alert.project_id, down=alert.down, code=exc.reason_code)
            continue
        sent += 1
        say(log, "alert_sent", project=alert.project_id, down=alert.down)
    return sent


__all__ = ["DEFAULT_THRESHOLD", "REASON_WORDS", "Alert", "Pusher", "UptimeWatch", "deliver"]
