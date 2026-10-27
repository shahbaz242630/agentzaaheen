"""Monitoring's certificate and domain dates: what changed since it last wrote something down (F11 slice 5b).

Code only, no model. Each run gets the newest read of one kind (certificates
or domains), the names that belong to this project, and the bookmark of how
they stood last time. Each name falls in one band:

- **expired or not valid** (a certificate that has ended, is for another
  name or is not trusted; a domain that has ended): an incident;
- **ends within 14 days:** a certificate's automatic renewal looks stuck, or a
  domain is about to lapse (risk P2);
- **ends within 30 days:** the role pack's "escalate within 30 days". A
  domain must be renewed (risk P2). A certificate is normally renewed
  automatically about now, so it is a heads-up (risk P3);
- **could not be read** (no answer, no date, or not in the read): risk P3,
  never fine;
- otherwise fine.

The bookmark holds the bands as a fingerprint (never a name), so a date that
only moves a day closer inside the same band is not written again; moving into
a nearer band is. A project with no names of this kind writes nothing (its
uptime watch already says when it has no addresses). Not read for longer than
``fresh`` is never fine (``_shared/absent-is-not-zero.md``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta

from agentzaaheen.core.sdk import Evidence, ExpiryCheck, ExpiryKind, ExpiryReport, FindingKind, Severity
from agentzaaheen.plugins.agents.monitoring.uptime import UNLISTED, UNREAD, UP, Note, Watched, at

CERTS_BOOKMARK = "certs.state"
DOMAINS_BOOKMARK = "domains.state"
CERTS_FRESH = timedelta(hours=13)  # read every 6 hours: two missed reads
DOMAINS_FRESH = timedelta(hours=49)  # read once a day: two missed reads
SOON = timedelta(days=30)  # the role pack's authority file: escalate within 30 days
STUCK = timedelta(days=14)
_LOOK = "look:"
_MAX_NAME = 60
_NOUN = {ExpiryKind.CERT: "certificates", ExpiryKind.DOMAIN: "domains"}
_BAD = "bad"
_D14 = "d14"
_D30 = "d30"
_UNREAD = "unread"
_NOT_VALID = {
    "cert.expired": "the certificate has expired",
    "cert.wrong_name": "the certificate is for another name",
    "cert.invalid": "the certificate is not trusted",
}


def watch_expiry(  # noqa: PLR0913  # which project, the read, its names, the time, and the bookmark
    project: str,
    name: str,
    report: ExpiryReport | None,
    *,
    kind: ExpiryKind,
    names: Sequence[str],
    now: datetime,
    seen: str | None,
    fresh: timedelta,
) -> Watched:
    """How ``project``'s certificates or domains stand now against ``seen``; a note only when that changed."""
    shown = name[:_MAX_NAME]
    noun = _NOUN[kind]
    listed = tuple(dict.fromkeys(names))
    if not listed:
        return Watched(None, UNLISTED)
    if report is None or now - report.fetched_at > fresh:
        state = UNREAD
        when = "no read yet" if report is None else f"the newest read is from {at(report.fetched_at)}"
        note = Note(
            FindingKind.RISK,
            Severity.P2,
            f"{shown}: {noun} dates could not be read",
            f"The {noun} were not read: {when}. Not read is never counted as fine; "
            "check the dashboard's log for the reason.",
            (Evidence(metric_id=f"{kind.value}.{project}"),),
        )
    else:
        found = {check.name: check for check in report.checks}
        bands = {n: _band(found.get(n), now) for n in sorted(listed)}
        look = {n: band for n, band in bands.items() if band is not None}
        if look:
            state = _LOOK + _fingerprint(look)
            note = _look_note(shown, kind, report, look, found, now=now, total=len(listed))
        else:
            state = UP
            note = Note(
                FindingKind.INFO,
                Severity.P3,
                f"{shown}: all {len(listed)} {noun} fine again",
                f"Every one was valid for more than 30 days at {at(report.fetched_at)}.",
                (Evidence(metric_id=f"{kind.value}.{project}"),),
            )
    if state == seen or (state == UP and seen is None):
        return Watched(None, state)
    return Watched(note, state)


def _band(check: ExpiryCheck | None, now: datetime) -> str | None:
    """Which band a name is in; ``None`` when it is fine."""
    if check is not None and check.reason in _NOT_VALID:
        return _BAD
    if check is None or check.expires_at is None:  # no answer, no date, or not in the read: never fine
        return _UNREAD
    left = check.expires_at - now
    for band, within in ((_BAD, timedelta(0)), (_D14, STUCK), (_D30, SOON)):
        if left <= within:
            return band
    return None


_ADVICE = {
    (ExpiryKind.DOMAIN, _D14): "renew it",
    (ExpiryKind.DOMAIN, _D30): "renew it",
    (ExpiryKind.CERT, _D14): "automatic renewal looks stuck; check the host's certificate settings",
    (ExpiryKind.CERT, _D30): "normally renewed automatically about now; nothing to do yet",
}


def _said(check: ExpiryCheck | None, band: str, now: datetime) -> str:
    if check is None:
        return "could not be read: it was not in the newest read"
    if check.reason in _NOT_VALID:
        return _NOT_VALID[check.reason]
    if check.expires_at is None:
        return f"could not be read ({check.reason})"
    ends = f"{check.expires_at:%d %b %Y}"
    if band == _BAD:
        return f"ended {ends}"
    return f"ends {ends} ({(check.expires_at - now).days} days): {_ADVICE[check.kind, band]}"


def _look_note(  # noqa: PLR0913  # the project, the kind, the read, what needs a look, the time, how many
    shown: str,
    kind: ExpiryKind,
    report: ExpiryReport,
    look: dict[str, str],
    found: dict[str, ExpiryCheck],
    *,
    now: datetime,
    total: int,
) -> Note:
    noun = _NOUN[kind]
    lines = [f"- {n}: {_said(found.get(n), band, now)}" for n, band in look.items()]
    body = f"Read at {at(report.fetched_at)}. Needs a look:\n" + "\n".join(lines)
    evidence = tuple(Evidence(url=f"https://{n}") for n in look)
    bad = sum(1 for band in look.values() if band == _BAD)
    if bad:
        return Note(
            FindingKind.INCIDENT, Severity.P2, f"{shown}: {bad} {noun} expired or not valid", body, evidence
        )
    urgent = any(band == _D14 for band in look.values()) or (
        kind is ExpiryKind.DOMAIN and _D30 in look.values()
    )
    severity = Severity.P2 if urgent else Severity.P3
    return Note(
        FindingKind.RISK, severity, f"{shown}: {len(look)} of {total} {noun} need a look", body, evidence
    )


def _fingerprint(look: dict[str, str]) -> str:
    """Which names are in which band, as a code: a change of band is a new note."""
    joined = "\n".join(f"{n}={band}" for n, band in sorted(look.items()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
