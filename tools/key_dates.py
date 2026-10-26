"""The key list: every key Agent Zaaheen holds, when it ends and how to renew it (never the key itself).

Keys live in several places (Windows user settings on the laptop, the laptop's
vault, the server's encrypted credentials, Cloudflare), so no one place can read
all their end dates. This list, given with ``--keys FILE``, names each one:

    - {name: Paddle (Zaaheen, live, read-only), where: laptop + server, expires: 2027-01-16,
       renew: "Paddle > Developer tools > Authentication: make a read-only key, then tell Claude"}
    - {name: Agent Zaaheen server model key, where: laptop vault + server, expires: never, renew: "-"}

The morning briefing then says, from code, which keys end within
:data:`~agentzaaheen.core.metrics.keys.WARN_BEFORE` (14 days) or have already
ended, soonest first. The list is local (``projects/`` is git-ignored) and holds
no secret: names, places, dates and plain instructions only, with no links (the
briefing never carries links).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agentzaaheen.core.metrics.keys import WARN_BEFORE
from agentzaaheen.core.sdk import get_logger, say
from tools.dashboard_sources import ListNotUsableError

MAX_LIST_BYTES = 16 * 1024
MAX_KEYS = 60
MAX_LINES = 4  # key lines in one briefing; the rest are counted


class KeyListError(Exception):
    """The key list cannot be used; the message says why in plain words."""


class KeyDate(BaseModel):
    """One key: what it is, where it lives, when it ends (or ``never``), how to renew it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    where: str = Field(min_length=1, max_length=60)
    expires: date | Literal["never"]
    renew: str = Field(min_length=1, max_length=160)

    @field_validator("name", "where", "renew")
    @classmethod
    def _plain(cls, value: str) -> str:
        if not value.isprintable():
            raise ValueError("one line of plain text, please")
        if "://" in value or "www." in value.lower():
            raise ValueError("no web addresses (the briefing never carries links)")
        return value


def load_key_dates(path: Path) -> tuple[KeyDate, ...]:
    """Every key in ``path``; refused whole if any entry is not usable."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise KeyListError(f"the key list {path} could not be read") from None
    if len(raw) > MAX_LIST_BYTES:
        raise KeyListError("the key list is too big")
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise KeyListError("the key list is not valid YAML") from None
    except ValueError:  # YAML reads 2027-13-40 as a date and fails on it
        raise KeyListError("the key list has a date that does not exist (write it as YYYY-MM-DD)") from None
    if not isinstance(data, list) or not data:
        raise KeyListError("the key list must be a list of keys")
    if len(data) > MAX_KEYS:
        raise KeyListError(f"the key list has more than {MAX_KEYS} keys")
    keys: list[KeyDate] = []
    for number, entry in enumerate(data, start=1):
        try:
            keys.append(KeyDate.model_validate(entry))
        except ValidationError as exc:
            first = exc.errors()[0]
            field = str(first["loc"][0]) if first["loc"] else "entry"  # "expires", not "expires.date"
            raise KeyListError(f"key {number}: {field} is not usable ({first['msg']})") from None
    return tuple(keys)


def key_dates_list(path: Path | None) -> tuple[KeyDate, ...]:
    """The key list given with ``--keys`` (empty without one); a broken list is said and raised."""
    if path is None:
        return ()
    log = get_logger("agentzaaheen.settings")
    try:
        found = load_key_dates(path)
    except KeyListError as exc:
        print(f"dashboard: {exc}.")
        say(log, "settings_refused", setting="keys")
        raise ListNotUsableError from None
    say(log, "settings_loaded", setting="keys", entries=len(found))
    return found


def key_lines(keys: Sequence[KeyDate], day: date) -> tuple[str, ...]:
    """The briefing's key lines for ``day``: ended or ending within 14 days, soonest first."""
    due = sorted(
        (
            (k.expires, k)
            for k in keys
            if isinstance(k.expires, date) and (k.expires - day).days <= WARN_BEFORE.days
        ),
        key=lambda pair: (pair[0], pair[1].name),
    )
    lines = [_line(key, ends, day) for ends, key in due[:MAX_LINES]]
    if len(due) > MAX_LINES:
        more = len(due) - MAX_LINES
        lines.append(f"... and {more} more {'key ends' if more == 1 else 'keys end'} soon; see the key list.")
    return tuple(lines)


def _line(key: KeyDate, ends: date, day: date) -> str:
    left = (ends - day).days
    when = f"{ends.day} {ends.strftime('%b %Y')}"
    if left < 0:
        head = f"Key ENDED {when}"
    elif left == 0:
        head = "Key ends TODAY"
    else:
        head = f"Key ends in {left} {'day' if left == 1 else 'days'} ({when})"
    return f"{head}: {key.name} ({key.where}). Renew: {key.renew}"
