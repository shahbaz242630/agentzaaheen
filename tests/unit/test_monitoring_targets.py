"""Monitoring's uptime targets: each band change written once, no target never (F07 step 5b)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentzaaheen.core.sdk import FindingKind, Severity, TargetStanding
from agentzaaheen.plugins.agents.monitoring import TARGETS_FRESH, UNLISTED, UNREAD, UP, Watched, watch_targets

NOW = datetime(2026, 10, 27, 17, 0, tzinfo=UTC)


def standing(
    up: int | None = 9990, left: int | None = 8000, *, burning: bool = False, read_at: datetime = NOW
) -> TargetStanding:
    return TargetStanding(
        objective="eva-availability",
        target_bp=9950,
        up_bp=up,
        budget_left_bp=left,
        burning=burning,
        checks=40_000,
        read_at=read_at,
    )


def watched(read: TargetStanding | None, seen: str | None = None, *, listed: bool = True) -> Watched:
    return watch_targets("eva", "Eva", read, listed=listed, now=NOW, seen=seen)


def test_a_project_without_a_target_writes_nothing() -> None:
    assert watched(standing(0, -100), listed=False) == watched(None, listed=False)
    result = watched(None, listed=False)
    assert (result.note, result.state) == (None, UNLISTED)


@pytest.mark.parametrize(
    ("read", "why"),
    [
        (None, "no read yet"),
        (standing(read_at=NOW - TARGETS_FRESH - timedelta(minutes=1)), "the newest read is from"),
        (standing(None, None), "no checks were counted in its window"),
    ],
)
def test_not_read_is_never_fine(read: TargetStanding | None, why: str) -> None:
    result = watched(read)
    assert result.state == UNREAD
    assert result.note is not None
    assert (result.note.kind, result.note.severity) == (FindingKind.RISK, Severity.P3)
    assert result.note.title == "Eva: uptime target could not be read"
    assert why in result.note.body


@pytest.mark.parametrize(
    ("read", "state", "severity", "title"),
    [
        (standing(9700, -400), "spent", Severity.P2, "Eva: uptime target missed, error budget spent"),
        (
            standing(9950, 0),
            "spent",
            Severity.P2,
            "Eva: uptime target missed, error budget spent",
        ),  # exactly used up
        (
            standing(9990, 6000, burning=True),
            "burning",
            Severity.P2,
            "Eva: spending its uptime budget too fast",
        ),
        (standing(9960, 2000), "low", Severity.P3, "Eva: under a quarter of its uptime budget left"),
    ],
)
def test_each_band_is_written_once(read: TargetStanding, state: str, severity: Severity, title: str) -> None:
    first = watched(read)
    assert first.state == state
    assert first.note is not None
    assert (first.note.kind, first.note.severity, first.note.title) == (FindingKind.RISK, severity, title)
    assert "of 40,000 external checks found it up, against a target of 99.50%" in first.note.body
    assert watched(read, seen=state).note is None  # the same band again: nothing new


def test_fine_on_the_first_run_writes_nothing_and_back_to_fine_is_said() -> None:
    assert watched(standing()) == watched(standing(), seen=UP)
    assert watched(standing()).note is None
    back = watched(standing(), seen="spent")
    assert back.state == UP
    assert back.note is not None
    assert (back.note.kind, back.note.title) == (FindingKind.INFO, "Eva: back within its uptime target")
