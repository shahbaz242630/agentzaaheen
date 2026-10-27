"""Monitoring's certificate and domain dates: a note only on a change of band (F11 slice 5b)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentzaaheen.core.sdk import Evidence, ExpiryCheck, ExpiryKind, ExpiryReport, FindingKind, Severity
from agentzaaheen.plugins.agents.monitoring import CERTS_FRESH, UNLISTED, UNREAD, UP, Watched, watch_expiry

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
HOSTS = ("sanduqkin.com", "sanduqkin-api.vercel.app")


def cert(name: str, days: float | None = 80, reason: str = "cert.ok") -> ExpiryCheck:
    ends = NOW + timedelta(days=days) if days is not None and reason == "cert.ok" else None
    return ExpiryCheck(kind=ExpiryKind.CERT, name=name, expires_at=ends, reason=reason)


def domain(name: str, days: float | None = 900, reason: str = "domain.ok") -> ExpiryCheck:
    ends = NOW + timedelta(days=days) if days is not None and reason == "domain.ok" else None
    return ExpiryCheck(kind=ExpiryKind.DOMAIN, name=name, expires_at=ends, reason=reason)


def read(*checks: ExpiryCheck, kind: ExpiryKind = ExpiryKind.CERT, at: datetime = NOW) -> ExpiryReport:
    return ExpiryReport(kind=kind, checks=checks, fetched_at=at)


def check(
    found: ExpiryReport | None,
    seen: str | None,
    *,
    kind: ExpiryKind = ExpiryKind.CERT,
    names: tuple[str, ...] = HOSTS,
) -> Watched:
    return watch_expiry(
        "sandoq-kin", "Sandoq Kin", found, kind=kind, names=names, now=NOW, seen=seen, fresh=CERTS_FRESH
    )


def test_the_first_run_with_every_date_far_off_writes_nothing() -> None:
    assert check(read(cert(HOSTS[0]), cert(HOSTS[1]), cert("other.com", 1)), None) == Watched(None, UP)


def test_a_certificate_within_30_days_is_a_heads_up_with_its_date() -> None:
    watched = check(read(cert(HOSTS[0], 22), cert(HOSTS[1])), UP)
    note = watched.note
    assert note is not None
    assert (note.kind, note.severity) == (FindingKind.RISK, Severity.P3)
    assert note.title == "Sandoq Kin: 1 of 2 certificates need a look"
    assert (
        "- sanduqkin.com: ends 18 Nov 2026 (22 days): normally renewed automatically about now" in note.body
    )
    assert note.evidence == (Evidence(url="https://sanduqkin.com"),)


def test_a_certificate_within_14_days_looks_stuck_and_is_said_again() -> None:
    soon = check(read(cert(HOSTS[0], 22), cert(HOSTS[1])), UP)
    later = check(read(cert(HOSTS[0], 21), cert(HOSTS[1])), soon.state)
    stuck = check(read(cert(HOSTS[0], 13), cert(HOSTS[1])), soon.state)
    assert later == Watched(None, soon.state)  # a day closer in the same band: not written again
    assert stuck.note is not None
    assert stuck.note.severity is Severity.P2
    assert "automatic renewal looks stuck" in stuck.note.body
    assert stuck.state != soon.state


@pytest.mark.parametrize(
    ("given", "said"),
    [
        (cert(HOSTS[0], -1), "ended "),
        (cert(HOSTS[0], reason="cert.expired"), "the certificate has expired"),
        (cert(HOSTS[0], reason="cert.wrong_name"), "the certificate is for another name"),
        (cert(HOSTS[0], reason="cert.invalid"), "the certificate is not trusted"),
    ],
)
def test_expired_or_not_valid_is_an_incident(given: ExpiryCheck, said: str) -> None:
    watched = check(read(given, cert(HOSTS[1])), UP)
    assert watched.note is not None
    assert (watched.note.kind, watched.note.severity) == (FindingKind.INCIDENT, Severity.P2)
    assert watched.note.title == "Sandoq Kin: 1 certificates expired or not valid"
    assert f"- sanduqkin.com: {said}" in watched.note.body


@pytest.mark.parametrize(
    "found",
    [
        read(cert(HOSTS[0], reason="cert.no_answer"), cert(HOSTS[1])),
        read(cert(HOSTS[1])),  # listed but not in the read
    ],
)
def test_a_name_that_could_not_be_read_is_never_fine(found: ExpiryReport) -> None:
    watched = check(found, UP)
    assert watched.note is not None
    assert (watched.note.kind, watched.note.severity) == (FindingKind.RISK, Severity.P3)
    assert "- sanduqkin.com: could not be read" in watched.note.body


def test_back_to_fine_is_said_once() -> None:
    soon = check(read(cert(HOSTS[0], 22), cert(HOSTS[1])), UP)
    fine = check(read(cert(HOSTS[0], 89), cert(HOSTS[1])), soon.state)
    assert fine.note is not None
    assert (fine.note.kind, fine.note.title) == (
        FindingKind.INFO,
        "Sandoq Kin: all 2 certificates fine again",
    )
    assert fine.state == UP


def test_a_domain_within_30_days_must_be_renewed() -> None:
    watched = check(
        read(domain("sanduqkin.com", 25), kind=ExpiryKind.DOMAIN),
        UP,
        kind=ExpiryKind.DOMAIN,
        names=("sanduqkin.com",),
    )
    assert watched.note is not None
    assert (watched.note.kind, watched.note.severity) == (FindingKind.RISK, Severity.P2)
    assert watched.note.title == "Sandoq Kin: 1 of 1 domains need a look"
    assert "(25 days): renew it" in watched.note.body


def test_a_domain_with_no_date_is_never_fine() -> None:
    watched = check(
        read(domain("sanduqkin.com", reason="domain.no_date"), kind=ExpiryKind.DOMAIN),
        UP,
        kind=ExpiryKind.DOMAIN,
        names=("sanduqkin.com",),
    )
    assert watched.note is not None
    assert "could not be read (domain.no_date)" in watched.note.body


@pytest.mark.parametrize(
    "found", [None, read(cert(HOSTS[0]), cert(HOSTS[1]), at=NOW - CERTS_FRESH - timedelta(1))]
)
def test_not_read_or_too_old_is_never_fine(found: ExpiryReport | None) -> None:
    watched = check(found, UP)
    assert watched.state == UNREAD
    assert watched.note is not None
    assert (watched.note.kind, watched.note.severity) == (FindingKind.RISK, Severity.P2)
    assert watched.note.title == "Sandoq Kin: certificates dates could not be read"
    assert check(found, UNREAD) == Watched(None, UNREAD)


def test_a_read_exactly_fresh_still_counts() -> None:
    assert check(read(cert(HOSTS[0]), cert(HOSTS[1]), at=NOW - CERTS_FRESH), UP) == Watched(None, UP)


def test_a_project_with_no_names_writes_nothing() -> None:
    assert check(read(cert(HOSTS[0])), None, names=()) == Watched(None, UNLISTED)


def test_the_bookmark_holds_a_fingerprint_never_a_name() -> None:
    watched = check(read(cert(HOSTS[0], 22), cert(HOSTS[1])), UP)
    assert "sanduqkin" not in watched.state
    assert len(watched.state) <= 256
