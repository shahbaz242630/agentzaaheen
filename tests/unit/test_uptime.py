"""Uptime (F03 slice 4): the rules, the connector, the probe that never reads a page, the card, the list."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tools import dashboard
from tools.dashboard_uptime import UptimeListError, load_uptime
from yaqzan.core.api.uptime import SLOW_MS, uptime_card
from yaqzan.core.metrics import Freshness
from yaqzan.core.net import NetError, NetErrorCode, StatusProbe
from yaqzan.core.sdk import Expect, SiteCheck, SiteRule, StatusAnswer, UptimeReport, check_site, judge
from yaqzan.plugins.connectors.uptime import FakeStatusGetter, UptimeSource

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SITE = "https://site.example"
API = "https://api.example"
HOUR = timedelta(hours=1)


@pytest.mark.parametrize(
    ("expect", "status", "up", "reason"),
    [
        (Expect.OK, 200, True, "uptime.ok"),
        (Expect.OK, 204, True, "uptime.ok"),
        (Expect.OK, 302, True, "uptime.redirect"),
        (Expect.OK, 404, False, "uptime.not_found"),
        (Expect.OK, 401, False, "uptime.refused"),
        (Expect.OK, 500, False, "uptime.server_error"),
        (Expect.OK, 503, False, "uptime.server_error"),
        (Expect.ANSWER, 404, True, "uptime.answered"),
        (Expect.ANSWER, 403, True, "uptime.answered"),
        (Expect.ANSWER, 200, True, "uptime.ok"),
        (Expect.ANSWER, 502, False, "uptime.server_error"),
    ],
)
def test_each_rule_judges_a_status(expect: Expect, status: int, up: bool, reason: str) -> None:
    assert judge(SiteRule(url=SITE, expect=expect), status) == (up, reason)


def test_no_answer_is_down() -> None:
    check = check_site(SiteRule(url=SITE), None, NOW)
    assert (check.up, check.reason, check.status, check.latency_ms) == (False, "uptime.no_answer", None, None)


@pytest.mark.parametrize(
    "url", ["http://site.example", "https://site.example?x=1", "https://user@site.example", "https://1.2.3.4"]
)
def test_only_plain_https_addresses_by_name_are_rules(url: str) -> None:
    with pytest.raises(ValueError, match=r"should match pattern|not an IP address"):
        SiteRule(url=url)


def test_the_source_checks_every_address_even_when_one_does_not_answer() -> None:
    fake = FakeStatusGetter({SITE: 200, API: 404})
    rules = [
        SiteRule(url="https://gone.example"),
        SiteRule(url=SITE),
        SiteRule(url=API, expect=Expect.ANSWER),
    ]
    report = asyncio.run(UptimeSource(fake, rules).fetch(now=NOW))
    assert [(s.url, s.up) for s in report.sites] == [
        ("https://gone.example", False),
        (SITE, True),
        (API, True),
    ]
    assert report.up == 2
    assert fake.asked == ["https://gone.example", SITE, API]
    assert report.fetched_at == NOW


def test_a_source_needs_addresses() -> None:
    with pytest.raises(ValueError, match="at least one"):
        UptimeSource(FakeStatusGetter({}), [])


# ---------------------------------------------------------------- the card


def report(*checks: SiteCheck) -> UptimeReport:
    return UptimeReport(sites=checks, fetched_at=NOW)


def check(url: str, status: int | None, ms: int = 100, expect: Expect = Expect.OK) -> SiteCheck:
    answer = None if status is None else StatusAnswer(status=status, elapsed_ms=ms)
    return check_site(SiteRule(url=url, expect=expect), answer, NOW)


def test_all_up_says_so() -> None:
    card = uptime_card(
        report(check(SITE, 200, 300), check(API, 404, 900, Expect.ANSWER)), schedule=HOUR, now=NOW
    )
    assert [(m.label, m.value.model_dump()) for m in card.metrics] == [
        ("Up", {"kind": "count", "value": 2}),
        ("Down", {"kind": "count", "value": 0}),
        ("Slowest answer (ms)", {"kind": "count", "value": 900}),
    ]
    assert card.metrics[0].detail == "of 2 addresses"
    assert [(e.text, e.tone.value) for e in card.entries] == [
        ("All 2 addresses answered as they should", "ok")
    ]
    assert (card.id, card.sample, card.freshness) == ("uptime", False, Freshness.FRESH)


def test_each_down_address_says_why_in_plain_words() -> None:
    card = uptime_card(
        report(check(SITE + "/health", 404), check(API, None), check("https://err.example", 503)),
        schedule=HOUR,
        now=NOW,
    )
    assert [e.text for e in card.entries] == [
        "site.example/health is down: page not found (http 404)",
        "api.example is down: did not answer",
        "err.example is down: server error (http 503)",
    ]
    assert {e.tone.value for e in card.entries} == {"alert"}


def test_a_slow_address_is_flagged() -> None:
    card = uptime_card(report(check(SITE, 200, SLOW_MS + 1)), schedule=HOUR, now=NOW)
    assert [(e.text, e.tone.value) for e in card.entries] == [
        (f"site.example is slow: {SLOW_MS + 1} ms", "warn")
    ]


def test_nothing_answered_means_the_slowest_is_unknown() -> None:
    card = uptime_card(report(check(SITE, None)), schedule=HOUR, now=NOW)
    assert card.metrics[2].value.model_dump() == {"kind": "unknown", "reason": "uptime.no_answer"}


def test_an_old_check_turns_every_number_unknown() -> None:
    card = uptime_card(report(check(SITE, 200)), schedule=HOUR, now=NOW + timedelta(hours=3))
    assert card.freshness is not Freshness.FRESH
    assert {m.value.kind for m in card.metrics} == {"unknown"}
    assert card.entries == ()


# ---------------------------------------------------------------- the probe


class NeverRead(httpx.AsyncByteStream):
    """A page body that fails the test if anyone reads it."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise AssertionError("the probe read the page")
        yield b""  # pragma: no cover

    async def aclose(self) -> None:
        return None


def probe(handler: object, hosts: frozenset[str] = frozenset({"site.example"})) -> StatusProbe:
    ticks = iter([1.0, 1.25])
    return StatusProbe(hosts, transport=httpx.MockTransport(handler), clock=lambda: next(ticks))  # type: ignore[arg-type]


def test_the_probe_reads_only_the_status_and_times_it() -> None:
    seen: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=NeverRead())

    answer = asyncio.run(probe(serve).status_of("https://site.example/health"))
    assert answer == StatusAnswer(status=200, elapsed_ms=250)
    (request,) = seen
    assert (request.method, str(request.url)) == ("GET", "https://site.example/health")
    assert request.headers["User-Agent"] == "Yaqzan-uptime/1"
    assert "authorization" not in {k.lower() for k in request.headers}


def test_a_redirect_is_reported_not_followed() -> None:
    seen: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/"}, stream=NeverRead())

    assert asyncio.run(probe(serve).status_of("https://site.example")).status == 302
    assert seen == ["https://site.example/"]


@pytest.mark.parametrize(
    "url",
    [
        "http://site.example",
        "https://other.example",
        "https://1.2.3.4/",
        "https://site.example:8443/",
        "https://site.example/?q=1",
        "https://user:pw@site.example/",
        "https://site.example/a/../b",
    ],
)
def test_the_probe_refuses_anything_off_its_rules_before_sending(url: str) -> None:
    def serve(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing may be sent")

    with pytest.raises(NetError) as caught:
        asyncio.run(probe(serve).status_of(url))
    assert caught.value.net_code is NetErrorCode.BLOCKED


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (httpx.ConnectTimeout("slow"), NetErrorCode.TIMEOUT),
        (httpx.ConnectError("refused"), NetErrorCode.UNAVAILABLE),
    ],
)
def test_no_answer_is_a_plain_network_error(failure: Exception, code: NetErrorCode) -> None:
    def serve(_request: httpx.Request) -> httpx.Response:
        raise failure

    with pytest.raises(NetError) as caught:
        asyncio.run(probe(serve).status_of("https://site.example"))
    assert caught.value.net_code is code


# ---------------------------------------------------------------- the local list and the dashboard

PROJECTS = {"zaaheen", "eva"}


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "uptime.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_list_maps_projects_to_rules(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        'eva:\n  - {url: "https://site.example/health"}\n  - {url: "https://api.example", expect: answer}\n',
    )
    rules = load_uptime(path, PROJECTS)
    assert rules == {
        "eva": (
            SiteRule(url="https://site.example/health"),
            SiteRule(url="https://api.example", expect=Expect.ANSWER),
        )
    }


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ('nobody:\n  - {url: "https://site.example"}\n', "unknown project"),
        ('eva:\n  - {url: "http://site.example"}\n', "not usable"),
        ('eva:\n  - {url: "https://site.example", expect: maybe}\n', "not usable"),
        ("eva: []\n", "give a list"),
        ("- just a list\n", "must map each project"),
        ("eva: [unclosed\n", "not valid YAML"),
        ("", "must map each project"),
        pytest.param("#" * 70_000, "too big", id="too-big"),  # a short id: pytest puts ids in an env var
    ],
)
def test_a_broken_list_says_why(tmp_path: Path, text: str, said: str) -> None:
    with pytest.raises(UptimeListError, match=said):
        load_uptime(write(tmp_path, text), PROJECTS)


def test_a_missing_list_says_so(tmp_path: Path) -> None:
    with pytest.raises(UptimeListError, match="could not be read"):
        load_uptime(tmp_path / "nope.yaml", PROJECTS)


@pytest.fixture
def ui(tmp_path: Path) -> Path:
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return tmp_path / "ui"


def test_the_dashboard_shows_real_uptime_cards(
    ui: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(
        tmp_path,
        'eva:\n  - {url: "https://site.example/health"}\n  - {url: "https://api.example", expect: answer}\n',
    )
    statuses = {"site.example": 404, "api.example": 404}

    def serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(statuses[request.url.host], stream=NeverRead())

    apps: list[object] = []
    code = dashboard.main(
        ["--uptime", str(path)],
        static_dir=ui,
        serve=lambda app, _h, _p: apps.append(app),
        transport=httpx.MockTransport(serve),
        clock=lambda: NOW,
    )
    assert code == 0
    assert "eva: 1 of 2 addresses up; down: https://site.example/health" in capsys.readouterr().out
    client = TestClient(apps[0], base_url="http://127.0.0.1")  # type: ignore[arg-type]
    card = next(c for c in client.get("/api/projects/eva").json()["cards"] if c["id"] == "uptime")
    assert (card["sample"], card["source"]) == (False, "Uptime (live)")
    assert card["entries"][0]["text"] == "site.example/health is down: page not found (http 404)"
    zaaheen = next(c for c in client.get("/api/projects/zaaheen").json()["cards"] if c["id"] == "uptime")
    assert zaaheen["sample"] is True  # not on the list: still the sample card
    uptime = next(c for c in client.get("/api/registry").json()["connectors"] if c["id"] == "uptime")
    assert uptime["uses"] == [{"project_id": "eva", "status": "live"}]


def test_a_broken_list_stops_the_start(ui: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, 'nobody:\n  - {url: "https://site.example"}\n')
    assert dashboard.main(["--uptime", str(path)], static_dir=ui, serve=lambda *_: None) == 1
    assert "unknown project" in capsys.readouterr().out


def test_without_a_list_uptime_is_not_in_use(ui: Path) -> None:
    apps: list[object] = []
    assert dashboard.main([], static_dir=ui, serve=lambda app, _h, _p: apps.append(app)) == 0
    client = TestClient(apps[0], base_url="http://127.0.0.1")  # type: ignore[arg-type]
    uptime = next(c for c in client.get("/api/registry").json()["connectors"] if c["id"] == "uptime")
    assert uptime["uses"] == []
