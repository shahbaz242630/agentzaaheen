"""Monitoring's uptime check: what changed since it last wrote something down (F11 slice 4).

Code only, no model (the role pack's "Tier 0"). Each run gets the newest
uptime check of one project and the bookmark of how it stood last time, and
says two things: the note to write down (or none) and the new bookmark.

- **Down:** which addresses (by part) and why, as an incident.
- **Back up:** every address up again after a down or an unread check.
- **Not read:** no check yet, or the newest check is older than ``fresh``.
  Not read is never "up" (``_shared/absent-is-not-zero.md``).
- **Nothing listed:** the project has no addresses to watch.

A state already written down is not written again: the bookmark holds the
state, and the down addresses only as a fingerprint (our own code, never an
address). The first run of a project that is all up writes nothing.

Phone alerts are not this agent's job: the uptime watch sends them within
minutes, without any agent.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from agentzaaheen.core.sdk import Evidence, FindingKind, Severity, SiteCheck, UptimeReport

BOOKMARK = "uptime.state"
FRESH = timedelta(minutes=15)  # three missed rounds of the 5-minute watch
UP = "up"
UNREAD = "unread"
UNLISTED = "unlisted"
_DOWN = "down:"
_MAX_NAME = 60


@dataclass(frozen=True)
class Note:
    """What Monitoring writes down (the runner turns it into a finding)."""

    kind: FindingKind
    severity: Severity
    title: str
    body: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class Watched:
    """One project's check: the note to write (``None``: nothing new) and the bookmark to keep."""

    note: Note | None
    state: str


def watch(  # noqa: PLR0913  # which project, its check, the time, and the bookmark
    project: str,
    name: str,
    report: UptimeReport | None,
    *,
    listed: bool,
    now: datetime,
    seen: str | None,
    fresh: timedelta = FRESH,
) -> Watched:
    """How ``project`` stands now against ``seen`` (its bookmark); a note only when that changed."""
    shown = name[:_MAX_NAME]
    metric = Evidence(metric_id=f"uptime.{project}")
    if not listed:
        state = UNLISTED
        note = Note(
            FindingKind.RISK,
            Severity.P3,
            f"{shown}: no addresses to watch",
            "This project has no addresses in the uptime list, so nobody would know if it went down. "
            "Add its addresses to projects/uptime.yaml.",
            (metric,),
        )
    elif report is None or now - report.fetched_at > fresh:
        state = UNREAD
        when = "no check yet" if report is None else f"the newest check is from {_at(report.fetched_at)}"
        note = Note(
            FindingKind.RISK,
            Severity.P2,
            f"{shown}: uptime could not be read",
            f"Uptime was not read: {when}. Not read is never counted as up; "
            "check that the dashboard's uptime watch is running.",
            (metric,),
        )
    else:
        down = [site for site in report.sites if not site.up]
        if down:
            state = _DOWN + _fingerprint(down)
            note = _down_note(shown, report, down)
        else:
            state = UP
            note = Note(
                FindingKind.INFO,
                Severity.P3,
                f"{shown}: all {len(report.sites)} addresses up again",
                f"Every address answered as it should at {_at(report.fetched_at)}.",
                (metric,),
            )
    if state == seen or (state == UP and seen is None):
        return Watched(None, state)
    return Watched(note, state)


def _down_note(shown: str, report: UptimeReport, down: list[SiteCheck]) -> Note:
    lines = [
        f"- {site.part or 'address'} ({site.url}): {site.reason}"
        + (f", answered {site.status}" if site.status is not None else ", no answer")
        for site in down
    ]
    body = f"Checked at {_at(report.fetched_at)}. Down:\n" + "\n".join(lines)
    return Note(
        FindingKind.INCIDENT,
        Severity.P2,
        f"{shown}: {len(down)} of {len(report.sites)} addresses down",
        body,
        tuple(Evidence(url=site.url) for site in down),
    )


def _fingerprint(down: list[SiteCheck]) -> str:
    """Which addresses are down, as a code: a different set of down addresses is a new note."""
    joined = "\n".join(sorted(site.url for site in down))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _at(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%d %b %H:%M UTC")
