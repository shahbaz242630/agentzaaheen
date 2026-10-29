"""Who is signed in to the dashboard (ADR-SEC-010): sessions in memory, and the wait after failed sign-ins.

A session is a random token in a cookie. Only its SHA-256 is kept, here in
memory, so a restart signs everyone out and nothing is stored that could be
stolen. A session ends after ``IDLE_AFTER`` with nothing done by a person, after
``LONGEST`` whatever happens, or when signed out. At most ``MAX_LIVE`` live at once;
a new one beyond that ends the oldest.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

IDLE_AFTER = timedelta(hours=3)
LONGEST = timedelta(hours=12)
MAX_LIVE = 3
TOKEN_BYTES = 32
FAILS_ALLOWED = 5
FAILS_WINDOW = timedelta(minutes=10)
WAIT_AFTER_FAILS = timedelta(minutes=1)


class SessionCheck(StrEnum):
    """What a token is worth now. ``IDLE`` and ``LIMIT`` mean it was live and has just ended."""

    LIVE = "session.live"
    NONE = "session.none"
    IDLE = "session.idle"
    LIMIT = "session.limit"


@dataclass
class _Live:
    started: datetime
    last_active: datetime


@dataclass(frozen=True)
class Started:
    """A new session's token (for the cookie only), and whether the oldest was ended to make room."""

    token: str
    ended_oldest: bool


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    """The live sessions, by the SHA-256 of their tokens."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._live: dict[str, _Live] = {}

    def start(self) -> Started:
        """A new session from now; the oldest is ended if ``MAX_LIVE`` are already live."""
        now = self._clock()
        ended_oldest = False
        if len(self._live) >= MAX_LIVE:
            oldest = min(self._live, key=lambda k: self._live[k].started)
            del self._live[oldest]
            ended_oldest = True
        token = secrets.token_urlsafe(TOKEN_BYTES)
        self._live[_key(token)] = _Live(started=now, last_active=now)
        return Started(token=token, ended_oldest=ended_oldest)

    def check(self, token: str | None, *, active: bool) -> SessionCheck:
        """Whether ``token`` is live now; ``active`` (a person did something) restarts the idle time."""
        if not token:
            return SessionCheck.NONE
        key = _key(token)
        live = self._live.get(key)
        if live is None:
            return SessionCheck.NONE
        now = self._clock()
        if now - live.started >= LONGEST:
            del self._live[key]
            return SessionCheck.LIMIT
        if now - live.last_active >= IDLE_AFTER:
            del self._live[key]
            return SessionCheck.IDLE
        if active:
            live.last_active = now
        return SessionCheck.LIVE

    def end(self, token: str | None) -> bool:
        """End the session ``token`` names; whether one was live."""
        return bool(token) and self._live.pop(_key(token or ""), None) is not None

    def live(self) -> int:
        """How many sessions are kept (some may have lapsed and not been checked since)."""
        return len(self._live)


class SignInWait:
    """After ``FAILS_ALLOWED`` failed sign-ins within ``FAILS_WINDOW``, sign-in waits ``WAIT_AFTER_FAILS``."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._fails: list[datetime] = []
        self._until: datetime | None = None

    def waiting(self) -> bool:
        """Whether sign-in must wait now."""
        if self._until is None:
            return False
        if self._clock() >= self._until:
            self._until = None
            return False
        return True

    def failed(self) -> bool:
        """Count one failure; whether this one starts the wait."""
        now = self._clock()
        self._fails = [at for at in self._fails if now - at < FAILS_WINDOW] + [now]
        if len(self._fails) >= FAILS_ALLOWED:
            self._fails = []
            self._until = now + WAIT_AFTER_FAILS
            return True
        return False

    def succeeded(self) -> None:
        """A good sign-in clears the count."""
        self._fails = []
