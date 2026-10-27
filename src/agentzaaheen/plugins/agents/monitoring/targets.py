"""Monitoring's uptime targets: what changed since it last wrote something down (F07 step 5b).

Code only, no model. Each run gets the project's standing against its uptime
target, worked out by code from the external checker's counts
(``TargetStanding``), and the bookmark of how it stood last time. It falls in
one band:

- **spent:** below target, or the error budget is used up (risk P2): reliability
  work comes first;
- **burning:** within target, but spending the budget too fast over the last
  3 days (risk P2);
- **low:** under a quarter of the budget left (risk P3): be careful with changes;
- **could not be read:** no standing, no checks counted, or older than
  ``fresh`` (risk P3), never fine (``_shared/absent-is-not-zero.md``);
- otherwise fine.

The bookmark holds the band, so a number that only moves inside the same band
is not written again; moving to another band is. A project without an uptime
target writes nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from agentzaaheen.core.sdk import Evidence, FindingKind, Severity, TargetStanding
from agentzaaheen.plugins.agents.monitoring.uptime import UNLISTED, UNREAD, UP, Note, Watched, at

BOOKMARK = "targets.state"
FRESH = timedelta(hours=3)  # read every hour: two missed reads and a margin
LOW_BP = 2_500  # a quarter of the budget, in basis points of it
_MAX_NAME = 60
_SPENT = "spent"
_BURNING = "burning"
_LOW = "low"


def watch_targets(  # noqa: PLR0913  # which project, its standing, whether it has a target, the time, the bookmark
    project: str,
    name: str,
    standing: TargetStanding | None,
    *,
    listed: bool,
    now: datetime,
    seen: str | None,
    fresh: timedelta = FRESH,
) -> Watched:
    """How ``project`` stands against its uptime target, against ``seen``; a note only when that changed."""
    if not listed:
        return Watched(None, UNLISTED)
    shown = name[:_MAX_NAME]
    evidence = (Evidence(metric_id=f"slo.{project}"),)
    if standing is None or now - standing.read_at > fresh or standing.up_bp is None:
        state = UNREAD
        why = (
            "no read yet"
            if standing is None
            else "no checks were counted in its window"
            if standing.up_bp is None
            else f"the newest read is from {at(standing.read_at)}"
        )
        note = Note(
            FindingKind.RISK,
            Severity.P3,
            f"{shown}: uptime target could not be read",
            f"The external checker's counts were not usable: {why}. Not read is never counted as fine.",
            evidence,
        )
    else:
        state = _band(standing)
        note = _note(shown, standing, state, evidence)
    if state == seen or (state == UP and seen is None):
        return Watched(None, state)
    return Watched(note, state)


def _band(standing: TargetStanding) -> str:
    left = standing.budget_left_bp
    below = standing.up_bp is not None and standing.up_bp < standing.target_bp
    if below or (left is not None and left <= 0):
        return _SPENT
    if standing.burning:
        return _BURNING
    if left is not None and left < LOW_BP:
        return _LOW
    return UP


def _pct(basis_points: int) -> str:
    whole, rest = divmod(basis_points, 100)
    return f"{whole}.{rest:02d}%"


def _note(shown: str, standing: TargetStanding, state: str, evidence: tuple[Evidence, ...]) -> Note:
    up = _pct(standing.up_bp or 0)
    read = (
        f"{up} of {standing.checks:,} external checks found it up, against a target of "
        f"{_pct(standing.target_bp)} ({standing.objective}; read {at(standing.read_at)})."
    )
    if state == _SPENT:
        return Note(
            FindingKind.RISK,
            Severity.P2,
            f"{shown}: uptime target missed, error budget spent",
            f"{read} Reliability work comes first: find what failed (the triage-outage skill), "
            "and keep changes small until the budget recovers.",
            evidence,
        )
    if state == _BURNING:
        return Note(
            FindingKind.RISK,
            Severity.P2,
            f"{shown}: spending its uptime budget too fast",
            f"{read} The last 3 days used the budget faster than the target allows; "
            "find the cause before it is spent.",
            evidence,
        )
    if state == _LOW:
        return Note(
            FindingKind.RISK,
            Severity.P3,
            f"{shown}: under a quarter of its uptime budget left",
            f"{read} Prefer small, careful changes until more of the budget is back.",
            evidence,
        )
    return Note(
        FindingKind.INFO,
        Severity.P3,
        f"{shown}: back within its uptime target",
        f"{read} The error budget is no longer spent or burning.",
        evidence,
    )
