"""Monitoring's Healthchecks check: a note only on a change; not read is never fine (F11 slice 5a)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentzaaheen.core.sdk import (
    CheckStatus,
    Evidence,
    FindingKind,
    HeartbeatCheck,
    HeartbeatReport,
    Severity,
)
from agentzaaheen.plugins.agents.monitoring import FRESH, UNLISTED, UNREAD, UP, Watched, watch_heartbeats

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
PINGED = datetime(2026, 10, 27, 8, 2, tzinfo=UTC)
SLUGS = ("zaaheen", "zaaheen-workers")


def hc(
    slug: str, status: CheckStatus = CheckStatus.UP, *, pinged: datetime | None = PINGED
) -> HeartbeatCheck:
    return HeartbeatCheck(slug=slug, status=status, last_ping=pinged)


def read(*checks: HeartbeatCheck, at: datetime = NOW) -> HeartbeatReport:
    return HeartbeatReport(checks=checks, fetched_at=at)


def check(found: HeartbeatReport | None, seen: str | None, slugs: tuple[str, ...] = SLUGS) -> Watched:
    return watch_heartbeats("zaaheen", "Zaaheen", found, slugs=slugs, now=NOW, seen=seen)


def test_the_first_run_with_every_check_fine_writes_nothing() -> None:
    watched = check(
        read(hc("zaaheen"), hc("zaaheen-workers", CheckStatus.GRACE), hc("other", CheckStatus.DOWN)), None
    )
    assert watched == Watched(None, UP)  # grace is not down yet; another project's check is not ours


def test_a_down_check_is_an_incident_naming_it_and_its_last_ping() -> None:
    watched = check(read(hc("zaaheen", CheckStatus.DOWN), hc("zaaheen-workers")), UP)
    note = watched.note
    assert note is not None
    assert (note.kind, note.severity) == (FindingKind.INCIDENT, Severity.P2)
    assert note.title == "Zaaheen: 1 of 2 Healthchecks checks down"
    assert "- zaaheen: down, last ping 27 Oct 08:02 UTC" in note.body
    assert note.evidence == (Evidence(metric_id="healthchecks.zaaheen"),)
    assert watched.state.startswith("look:")


def test_the_same_state_is_not_written_twice_but_a_new_one_is() -> None:
    down = check(read(hc("zaaheen", CheckStatus.DOWN), hc("zaaheen-workers")), UP)
    again = check(read(hc("zaaheen", CheckStatus.DOWN), hc("zaaheen-workers")), down.state)
    both = check(read(hc("zaaheen", CheckStatus.DOWN), hc("zaaheen-workers", CheckStatus.DOWN)), down.state)
    assert again == Watched(None, down.state)
    assert both.note is not None
    assert both.note.title == "Zaaheen: 2 of 2 Healthchecks checks down"
    assert both.state != down.state


def test_back_to_fine_is_said_once() -> None:
    down = check(read(hc("zaaheen", CheckStatus.DOWN), hc("zaaheen-workers")), UP)
    fine = check(read(hc("zaaheen"), hc("zaaheen-workers")), down.state)
    assert fine.note is not None
    assert (fine.note.kind, fine.note.title) == (
        FindingKind.INFO,
        "Zaaheen: all 2 Healthchecks checks fine again",
    )
    assert fine.state == UP


@pytest.mark.parametrize(
    ("status", "said"),
    [
        (CheckStatus.PAUSED, "zaaheen: paused, so nobody would be told if it stopped"),
        (CheckStatus.NEW, "zaaheen: never pinged"),
    ],
)
def test_paused_or_never_pinged_needs_a_look_but_is_not_an_incident(status: CheckStatus, said: str) -> None:
    watched = check(read(hc("zaaheen", status, pinged=None), hc("zaaheen-workers")), UP)
    assert watched.note is not None
    assert (watched.note.kind, watched.note.severity) == (FindingKind.RISK, Severity.P3)
    assert watched.note.title == "Zaaheen: 1 of 2 Healthchecks checks need a look"
    assert f"- {said}\n" in watched.note.body + "\n"  # no "last ping" when it never pinged


def test_a_check_missing_from_healthchecks_is_never_counted_as_fine() -> None:
    watched = check(read(hc("zaaheen")), UP)
    assert watched.note is not None
    assert "- zaaheen-workers: missing: Healthchecks did not return it" in watched.note.body


@pytest.mark.parametrize(
    "found", [None, read(hc("zaaheen"), hc("zaaheen-workers"), at=NOW - FRESH - timedelta(seconds=1))]
)
def test_not_read_or_too_old_is_never_fine(found: HeartbeatReport | None) -> None:
    watched = check(found, UP)
    assert watched.state == UNREAD
    assert watched.note is not None
    assert (watched.note.kind, watched.note.severity) == (FindingKind.RISK, Severity.P2)
    assert watched.note.title == "Zaaheen: Healthchecks could not be read"
    assert check(found, UNREAD) == Watched(None, UNREAD)


def test_a_read_exactly_fresh_still_counts() -> None:
    assert check(read(hc("zaaheen"), hc("zaaheen-workers"), at=NOW - FRESH), UP) == Watched(None, UP)


def test_a_project_with_no_checks_listed_is_said_once() -> None:
    watched = check(read(hc("zaaheen")), None, slugs=())
    assert watched.state == UNLISTED
    assert watched.note is not None
    assert watched.note.title == "Zaaheen: no Healthchecks checks to watch"
    assert check(read(hc("zaaheen")), UNLISTED, slugs=()) == Watched(None, UNLISTED)


def test_the_bookmark_holds_a_fingerprint_never_a_slug() -> None:
    watched = check(read(hc("zaaheen", CheckStatus.DOWN), hc("zaaheen-workers")), UP)
    assert "zaaheen" not in watched.state
    assert len(watched.state) <= 256
