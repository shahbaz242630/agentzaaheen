"""Certificate and domain dates on the dashboard: the domain list, and reads only when due (F11 slice 5b)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from agentzaaheen.core.net import CertProbe
from agentzaaheen.core.sdk import ExpiryKind, ExpiryReport, SiteRule, SourceError, SourceErrorCode
from tools.dashboard_expiry import (
    CERTS_EVERY,
    DOMAINS_EVERY,
    DomainsListError,
    ExpiryReader,
    cert_hosts,
    cert_reader,
    domain_reader,
    domains_list,
    load_domains,
)
from tools.dashboard_sources import ListNotUsableError

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
PROJECTS = {"zaaheen", "sandoq-kin", "eva"}


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "domains.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_list_maps_each_project_to_its_own_domains(tmp_path: Path) -> None:
    listed = load_domains(
        write(tmp_path, "zaaheen: [zaaheen.com, zaaheen.com]\nsandoq-kin: [sanduqkin.net]\n"), PROJECTS
    )
    assert listed == {"zaaheen": ("zaaheen.com",), "sandoq-kin": ("sanduqkin.net",)}


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("", "must map each project"),
        ("- zaaheen.com\n", "must map each project"),
        ("nearby: [nearby.com]\n", "unknown project"),
        ("eva: []\n", "give a list of 1 to 10"),
        ("eva: eva.com\n", "give a list of 1 to 10"),
        (f"eva: [{', '.join(f'e{i}.com' for i in range(11))}]\n", "give a list of 1 to 10"),
        ("eva: [Eva.com]\n", "not a lower-case domain name"),
        ("eva: [127.0.0.1]\n", "not a lower-case domain name"),
        (f"eva: [{'a' * 250}.com]\n", "not a lower-case domain name"),
        ("eva: [eva.co.uk]\n", "not a .com or .net domain"),
        ("eva: [eva.com\n", "not valid YAML"),
    ],
)
def test_a_broken_list_says_why(tmp_path: Path, text: str, said: str) -> None:
    with pytest.raises(DomainsListError, match=said):
        load_domains(write(tmp_path, text), PROJECTS)


def test_a_list_too_big_or_missing_says_why(tmp_path: Path) -> None:
    with pytest.raises(DomainsListError, match="too big"):
        load_domains(write(tmp_path, "#" * (16 * 1024 + 1)), PROJECTS)
    with pytest.raises(DomainsListError, match="could not be read"):
        load_domains(tmp_path / "missing.yaml", PROJECTS)


def test_no_list_is_empty_and_a_broken_one_stops_the_start_after_saying_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert domains_list(None, PROJECTS) == {}
    assert domains_list(write(tmp_path, "eva: [eva.com]\n"), PROJECTS) == {"eva": ("eva.com",)}
    with pytest.raises(ListNotUsableError):
        domains_list(write(tmp_path, "eva: [eva.co.uk]\n"), PROJECTS)
    assert "not a .com or .net domain" in capsys.readouterr().out


def test_each_project_s_certificate_hosts_come_from_its_uptime_addresses() -> None:
    rules = {
        "zaaheen": (
            SiteRule(url="https://zaaheen.com"),
            SiteRule(url="https://zaaheen.com/account/"),
            SiteRule(url="https://api.zaaheen.com/v1/web/plan", expect="status", status=405),
        ),
        "eva": (SiteRule(url="https://eva.up.railway.app/health"),),
    }
    assert cert_hosts(rules) == {
        "zaaheen": ("zaaheen.com", "api.zaaheen.com"),
        "eva": ("eva.up.railway.app",),
    }


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def test_a_reader_reads_only_when_due_and_keeps_the_last_good_read() -> None:
    clock, reads, failing = Clock(), [], [False]

    async def read(now: datetime) -> ExpiryReport:
        reads.append(now)
        if failing[0]:
            raise SourceError(SourceErrorCode.UNAVAILABLE, "down")
        return ExpiryReport(kind=ExpiryKind.CERT, checks=(), fetched_at=now)

    reader = ExpiryReader(ExpiryKind.CERT, read, every=CERTS_EVERY, clock=clock)
    asyncio.run(reader.refresh())
    first = reader.latest
    clock.now += CERTS_EVERY - timedelta(seconds=1)
    asyncio.run(reader.refresh())  # not due yet
    clock.now += timedelta(seconds=1)
    failing[0] = True
    asyncio.run(reader.refresh())  # due, but fails: the last read stays and turns old by itself
    assert reads == [NOW, NOW + CERTS_EVERY]
    assert reader.latest is first
    assert first is not None
    assert first.fetched_at == NOW


def test_nothing_listed_means_no_reader() -> None:
    assert cert_reader({"eva": ()}, clock=Clock()) is None
    assert domain_reader({}, clock=Clock()) is None


def test_the_certificate_reader_asks_each_host_once() -> None:
    asked: list[str] = []

    async def opener(host: str, *_: object) -> dict[str, object]:
        asked.append(host)
        return {"notAfter": "Jan 20 12:00:00 2027 GMT"}

    hosts = {"zaaheen": ("zaaheen.com", "api.zaaheen.com"), "eva": ("zaaheen.com",)}
    probe = CertProbe(frozenset({"zaaheen.com", "api.zaaheen.com"}), opener=opener)
    reader = cert_reader(hosts, clock=Clock(), probe=probe)
    assert reader is not None
    assert reader.kind is ExpiryKind.CERT
    asyncio.run(reader.refresh())
    assert asked == ["zaaheen.com", "api.zaaheen.com"]
    assert reader.latest is not None
    assert {c.expires_at for c in reader.latest.checks} == {datetime(2027, 1, 20, 12, 0, tzinfo=UTC)}


def test_the_domain_reader_asks_the_public_registry_for_each_domain_once_a_day() -> None:
    asked: list[httpx.Request] = []

    def registry(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(
            200, json={"events": [{"eventAction": "expiration", "eventDate": "2029-08-27T17:15:01Z"}]}
        )

    clock = Clock()
    reader = domain_reader(
        {"zaaheen": ("zaaheen.com",), "sandoq-kin": ("sanduqkin.com",)},
        clock=clock,
        transport=httpx.MockTransport(registry),
    )
    assert reader is not None
    asyncio.run(reader.refresh())
    clock.now += DOMAINS_EVERY - timedelta(minutes=1)
    asyncio.run(reader.refresh())
    assert [str(r.url) for r in asked] == [
        "https://rdap.verisign.com/com/v1/domain/zaaheen.com",
        "https://rdap.verisign.com/com/v1/domain/sanduqkin.com",
    ]
    assert all("authorization" not in r.headers for r in asked)  # no key: public data
    assert reader.latest is not None
    assert [c.reason for c in reader.latest.checks] == ["domain.ok", "domain.ok"]
