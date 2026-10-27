"""Monitoring's uptime check: a note only when the state changed; not read is never "up" (F11 slice 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentzaaheen.core.sdk import Evidence, Expect, FindingKind, Severity, SiteCheck, UptimeReport
from agentzaaheen.plugins.agents.monitoring import FRESH, UNLISTED, UNREAD, UP, Watched, watch

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
SITE = "https://eva.example.com"
API = "https://api.eva.example.com"


def site(url: str, *, up: bool = True, status: int | None = 200, part: str | None = None) -> SiteCheck:
    reason = "uptime.ok" if up else ("uptime.server_error" if status else "uptime.no_answer")
    return SiteCheck(
        url=url,
        expect=Expect.OK,
        part=part,
        up=up,
        status=status,
        latency_ms=80,
        reason=reason,
        checked_at=NOW,
    )


def report(*sites: SiteCheck, at: datetime = NOW) -> UptimeReport:
    return UptimeReport(sites=sites, fetched_at=at)


def check(found: UptimeReport | None, seen: str | None, *, listed: bool = True) -> Watched:
    return watch("eva", "Eva", found, listed=listed, now=NOW, seen=seen)


def test_the_first_run_of_a_project_that_is_all_up_writes_nothing() -> None:
    watched = check(report(site(SITE), site(API)), None)
    assert (watched.note, watched.state) == (None, UP)


def test_down_is_an_incident_naming_each_part_and_why() -> None:
    watched = check(report(site(SITE, part="site"), site(API, up=False, status=503, part="api")), UP)
    note = watched.note
    assert note is not None
    assert (note.kind, note.severity, note.title) == (
        FindingKind.INCIDENT,
        Severity.P2,
        "Eva: 1 of 2 addresses down",
    )
    assert "api (https://api.eva.example.com): uptime.server_error, answered 503" in note.body
    assert note.evidence == (Evidence(url=API),)
    assert watched.state.startswith("down:")
    assert API not in watched.state  # the bookmark holds a fingerprint, never an address


def test_no_answer_is_said_as_no_answer() -> None:
    note = check(report(site(SITE, up=False, status=None)), UP).note
    assert note is not None
    assert "no answer" in note.body


def test_the_same_down_is_not_written_again_but_a_different_one_is() -> None:
    first = check(report(site(SITE, up=False, status=503), site(API)), UP)
    same = check(report(site(SITE, up=False, status=502), site(API)), first.state)
    other = check(report(site(SITE), site(API, up=False, status=503)), first.state)
    assert same.note is None
    assert same.state == first.state
    assert other.note is not None
    assert other.state != first.state


@pytest.mark.parametrize("before", ["down:" + "a" * 64, UNREAD])
def test_back_up_after_down_or_unread_is_written_once(before: str) -> None:
    watched = check(report(site(SITE), site(API)), before)
    assert watched.note is not None
    assert (watched.note.kind, watched.note.severity) == (FindingKind.INFO, Severity.P3)
    assert watched.note.title == "Eva: all 2 addresses up again"
    assert check(report(site(SITE), site(API)), UP).note is None


@pytest.mark.parametrize(
    ("found", "said"),
    [
        (None, "no check yet"),
        (
            report(site(SITE), at=NOW - FRESH - timedelta(seconds=1)),
            "the newest check is from 27 Oct 08:44",
        ),
    ],
)
def test_not_read_is_never_up(found: UptimeReport | None, said: str) -> None:
    watched = check(found, UP)
    assert watched.state == UNREAD
    assert watched.note is not None
    assert (watched.note.kind, watched.note.severity) == (FindingKind.RISK, Severity.P2)
    assert said in watched.note.body
    assert watched.note.evidence == (Evidence(metric_id="uptime.eva"),)
    assert check(found, None).note is not None  # even on the first run
    assert check(found, UNREAD).note is None  # and said once


def test_a_check_just_inside_the_window_still_counts() -> None:
    assert check(report(site(SITE), at=NOW - FRESH), UP).state == UP


def test_a_project_with_no_addresses_is_said_once() -> None:
    watched = check(None, None, listed=False)
    assert watched.state == UNLISTED
    assert watched.note is not None
    assert watched.note.severity is Severity.P3
    assert check(None, UNLISTED, listed=False).note is None


def test_a_long_name_still_fits_a_title() -> None:
    watched = watch("eva", "E" * 200, report(site(SITE, up=False, status=500)), listed=True, now=NOW, seen=UP)
    assert watched.note is not None
    assert len(watched.note.title) <= 120
