"""Monitoring's Healthchecks check: what changed since it last wrote something down (F11 slice 5a).

Code only, no model. Each run gets the newest read of every Healthchecks
check, the slugs that belong to this project, and the bookmark of how they
stood last time. A check counts as fine only when Healthchecks says ``up``,
or ``grace`` (a ping is late but inside its grace time; Healthchecks has not
called it down). Everything else needs a look:

- **down:** Healthchecks stopped hearing pings (an incident);
- **paused:** nobody would be told if it stopped;
- **never pinged:** made but never heard from;
- **missing:** this project expects it, but Healthchecks did not return it.

Not read (no read yet, or older than ``fresh``) is never "fine"
(``_shared/absent-is-not-zero.md``). The bookmark holds the state, the
checks that need a look only as a fingerprint, so the same state is not
written again; the first run of a project whose checks are all fine writes
nothing. Phone alerts are not this agent's job: Healthchecks sends them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta

from agentzaaheen.core.sdk import (
    CheckStatus,
    Evidence,
    FindingKind,
    HeartbeatCheck,
    HeartbeatReport,
    Severity,
)
from agentzaaheen.plugins.agents.monitoring.uptime import FRESH, UNLISTED, UNREAD, UP, Note, Watched, at

BOOKMARK = "healthchecks.state"
_LOOK = "look:"
_MAX_NAME = 60
_FINE = frozenset({CheckStatus.UP, CheckStatus.GRACE})
_SAID = {
    CheckStatus.DOWN: "down",
    CheckStatus.PAUSED: "paused, so nobody would be told if it stopped",
    CheckStatus.NEW: "never pinged",
}
_MISSING = "missing: Healthchecks did not return it (removed, renamed, or in a state this code does not know)"


def watch_heartbeats(  # noqa: PLR0913  # which project, its checks, the read, the time, and the bookmark
    project: str,
    name: str,
    report: HeartbeatReport | None,
    *,
    slugs: Sequence[str],
    now: datetime,
    seen: str | None,
    fresh: timedelta = FRESH,
) -> Watched:
    """How ``project``'s Healthchecks checks stand now against ``seen``; a note only when that changed."""
    shown = name[:_MAX_NAME]
    metric = Evidence(metric_id=f"healthchecks.{project}")
    if not slugs:
        state = UNLISTED
        note = Note(
            FindingKind.RISK,
            Severity.P3,
            f"{shown}: no Healthchecks checks to watch",
            "This project has no Healthchecks checks on the list, so a job or the outside checker "
            "could stop without anyone reading it here. Add its checks to projects/healthchecks.yaml.",
            (metric,),
        )
    elif report is None or now - report.fetched_at > fresh:
        state = UNREAD
        when = "no read yet" if report is None else f"the newest read is from {at(report.fetched_at)}"
        note = Note(
            FindingKind.RISK,
            Severity.P2,
            f"{shown}: Healthchecks could not be read",
            f"Healthchecks was not read: {when}. Not read is never counted as fine; "
            "check the dashboard's Healthchecks key and its log.",
            (metric,),
        )
    else:
        found = {check.slug: check for check in report.checks}
        look = {
            slug: (_SAID[found[slug].status] if slug in found else _MISSING)
            for slug in sorted(set(slugs))
            if slug not in found or found[slug].status not in _FINE
        }
        if look:
            state = _LOOK + _fingerprint(look)
            note = _look_note(shown, report, look, found, total=len(set(slugs)))
        else:
            state = UP
            note = Note(
                FindingKind.INFO,
                Severity.P3,
                f"{shown}: all {len(set(slugs))} Healthchecks checks fine again",
                f"Every check was up (or inside its grace time) at {at(report.fetched_at)}.",
                (metric,),
            )
    if state == seen or (state == UP and seen is None):
        return Watched(None, state)
    return Watched(note, state)


def _look_note(
    shown: str, report: HeartbeatReport, look: dict[str, str], found: dict[str, HeartbeatCheck], *, total: int
) -> Note:
    lines = []
    for slug, said in look.items():
        check = found.get(slug)
        pinged = check.last_ping if check is not None else None
        lines.append(f"- {slug}: {said}" + (f", last ping {at(pinged)}" if pinged is not None else ""))
    body = f"Read at {at(report.fetched_at)}. Needs a look:\n" + "\n".join(lines)
    evidence = tuple(Evidence(metric_id=f"healthchecks.{slug}") for slug in look)
    down = sum(1 for slug in look if slug in found and found[slug].status is CheckStatus.DOWN)
    if down:
        title = f"{shown}: {down} of {total} Healthchecks checks down"
        return Note(FindingKind.INCIDENT, Severity.P2, title, body, evidence)
    title = f"{shown}: {len(look)} of {total} Healthchecks checks need a look"
    return Note(FindingKind.RISK, Severity.P3, title, body, evidence)


def _fingerprint(look: dict[str, str]) -> str:
    """Which checks need a look and why, as a code: a different set is a new note."""
    joined = "\n".join(f"{slug}={said}" for slug, said in sorted(look.items()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
