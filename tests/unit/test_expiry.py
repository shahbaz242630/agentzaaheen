"""The certificate and domain dates connector: one name at a time, never stopped by one (F11 slice 5b)."""

from __future__ import annotations

import asyncio
import pathlib
from datetime import UTC, datetime

import pytest
import yaml
from pydantic import ValidationError

from agentzaaheen.core.sdk import (
    CertAnswer,
    ExpiryCheck,
    ExpiryKind,
    ExpiryReport,
    SourceErrorCode,
)
from agentzaaheen.plugins.connectors.expiry import (
    ALLOWED_HOSTS,
    CertSource,
    DomainSource,
    FakeCertGetter,
    FakeRdap,
)

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
ENDS = datetime(2027, 1, 20, 12, 0, tzinfo=UTC)


def rdap(*events: dict[str, object]) -> dict[str, object]:
    return {"objectClassName": "domain", "ldhName": "ZAAHEEN.COM", "events": list(events)}


def test_each_host_is_read_once_and_one_without_an_answer_never_stops_the_others() -> None:
    getter = FakeCertGetter(
        {
            "zaaheen.com": CertAnswer(reason="cert.ok", not_after=ENDS),
            "api.zaaheen.com": CertAnswer(reason="cert.wrong_name"),
        }
    )
    report = asyncio.run(
        CertSource(getter, ["zaaheen.com", "api.zaaheen.com", "dl.zaaheen.com", "zaaheen.com"]).fetch(now=NOW)
    )
    assert report.kind is ExpiryKind.CERT
    assert [(c.name, c.reason, c.expires_at) for c in report.checks] == [
        ("zaaheen.com", "cert.ok", ENDS),
        ("api.zaaheen.com", "cert.wrong_name", None),
        ("dl.zaaheen.com", "cert.no_answer", None),
    ]
    assert getter.asked == ["zaaheen.com", "api.zaaheen.com", "dl.zaaheen.com"]


def test_each_domain_keeps_only_its_expiration_date() -> None:
    api = FakeRdap(
        {
            "/com/v1/domain/zaaheen.com": rdap(
                {"eventAction": "registration", "eventDate": "2026-08-27T17:15:01Z"},
                {"eventAction": "expiration", "eventDate": "2029-08-27T17:15:01Z"},
            ),
            "/net/v1/domain/example.net": rdap(
                {"eventAction": "registration", "eventDate": "2026-01-01T00:00:00Z"}
            ),
            "/com/v1/domain/gone.com": SourceErrorCode.REFUSED,
        }
    )
    report = asyncio.run(DomainSource(api, ["zaaheen.com", "example.net", "gone.com"]).fetch(now=NOW))
    assert [(c.name, c.reason, c.expires_at) for c in report.checks] == [
        ("zaaheen.com", "domain.ok", datetime(2029, 8, 27, 17, 15, 1, tzinfo=UTC)),
        ("example.net", "domain.no_date", None),
        ("gone.com", "domain.no_answer", None),
    ]
    assert [path for path, _ in api.asked] == [
        "/com/v1/domain/zaaheen.com",
        "/net/v1/domain/example.net",
        "/com/v1/domain/gone.com",
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"events": "not a list"},
        {},
        rdap({"eventAction": "expiration", "eventDate": 2029}),
        rdap({"eventAction": "expiration", "eventDate": "someday"}),
        rdap({"eventAction": "expiration", "eventDate": "2029-08-27T17:15:01"}),  # no zone
        rdap("not an event"),  # type: ignore[arg-type]
    ],
)
def test_an_expiration_we_cannot_trust_is_no_date(body: dict[str, object]) -> None:
    report = asyncio.run(
        DomainSource(FakeRdap({"/com/v1/domain/zaaheen.com": body}), ["zaaheen.com"]).fetch(now=NOW)
    )
    assert report.checks[0].reason == "domain.no_date"


@pytest.mark.parametrize("name", ["zaaheen.co.uk", "zaaheen.ae"])
def test_only_com_and_net_domains_can_be_read(name: str) -> None:
    with pytest.raises(ValueError, match=r"only \.com and \.net"):
        DomainSource(FakeRdap({}), [name])


@pytest.mark.parametrize("name", ["Zaaheen.com", "-bad.com", "localhost", "a b.com", "127.0.0.1"])
def test_a_name_that_is_not_a_lower_case_host_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="host name"):
        CertSource(FakeCertGetter({}), [name])


def test_more_than_a_hundred_names_is_refused() -> None:
    with pytest.raises(ValueError, match="at most 100"):
        CertSource(FakeCertGetter({}), [f"h{i}.zaaheen.com" for i in range(101)])


def test_a_check_and_a_report_keep_to_one_kind() -> None:
    with pytest.raises(ValidationError, match="its own kind"):
        ExpiryCheck(kind=ExpiryKind.CERT, name="zaaheen.com", reason="domain.ok")
    with pytest.raises(ValidationError, match="one kind"):
        ExpiryReport(
            kind=ExpiryKind.DOMAIN,
            checks=(ExpiryCheck(kind=ExpiryKind.CERT, name="zaaheen.com", reason="cert.ok"),),
            fetched_at=NOW,
        )


def test_the_manifest_reads_only_and_needs_no_key() -> None:
    import agentzaaheen.plugins.connectors.expiry as plugin  # noqa: PLC0415  # the folder it lives in

    manifest = yaml.safe_load(
        pathlib.Path(plugin.__file__).with_name("plugin.yaml").read_text(encoding="utf-8")
    )
    assert manifest["permissions"] == ["read"]
    assert manifest["credentials"] == []
    assert set(manifest["egress"]) >= ALLOWED_HOSTS  # the registry is one of the listed hosts
