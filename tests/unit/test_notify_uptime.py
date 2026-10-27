"""Uptime alerts (ADR-031): when to tell the Chairman, in fixed words, through a guarded ntfy push."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.net import NetError, NetErrorCode, PushClient
from agentzaaheen.core.notify import Alert, UptimeWatch, deliver
from agentzaaheen.core.sdk import (
    SiteRule,
    SourceError,
    SourceErrorCode,
    StatusAnswer,
    UptimeReport,
    check_site,
)
from tests.tracing import recorded
from tools.dashboard_uptime import UptimeLive, start_watch

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
TOPIC = SecretStr("abcdefghijklmnop" + "qrstuvwx")  # a made-up topic, 24 characters
A = "https://a.example"
B = "https://b.example/health"
NAMES = {"eva": "Eva", "zaaheen": "Zaaheen"}


def report(**status: int | None) -> UptimeReport:
    """``a=200, b=None``: address a answered 200, b did not answer."""
    urls = {"a": A, "b": B}
    sites = tuple(
        check_site(SiteRule(url=urls[k]), None if v is None else StatusAnswer(status=v, elapsed_ms=50), NOW)
        for k, v in status.items()
    )
    return UptimeReport(sites=sites, fetched_at=NOW)


def test_already_down_at_the_first_round_is_reported_once() -> None:
    watch = UptimeWatch(NAMES)
    (alert,) = watch.observe({"eva": report(a=404, b=200)})
    assert alert == Alert(
        project_id="eva",
        down=True,
        title="Agent Zaaheen: Eva is down",
        message="1 of 2 addresses down (page not found). Details on the dashboard.",
        priority="high",
    )
    assert watch.observe({"eva": report(a=404, b=200)}) == []  # no repeat while it stays down


def test_a_new_failure_needs_two_checks_in_a_row() -> None:
    watch = UptimeWatch(NAMES)
    assert watch.observe({"zaaheen": report(a=200, b=200)}) == []
    assert watch.observe({"zaaheen": report(a=200, b=None)}) == []  # one blip: quiet
    assert watch.observe({"zaaheen": report(a=200, b=200)}) == []  # recovered before the alert: still quiet
    assert watch.observe({"zaaheen": report(a=200, b=None)}) == []
    (alert,) = watch.observe({"zaaheen": report(a=503, b=None)})
    assert alert.title == "Agent Zaaheen: Zaaheen is down"
    # b is alerted (second fail in a row); a has failed once, so it counts as down but is not yet the reason.
    assert alert.message == "2 of 2 addresses down (did not answer). Details on the dashboard."


def test_back_up_is_said_once() -> None:
    watch = UptimeWatch(NAMES)
    watch.observe({"eva": report(a=404, b=500)})
    (alert,) = watch.observe({"eva": report(a=200, b=500)})
    assert (alert.down, alert.title, alert.priority) == (False, "Agent Zaaheen: Eva is back up", "default")
    assert alert.message == "1 address answering again; 1 of 2 up now."
    assert watch.observe({"eva": report(a=200, b=500)}) == []


def test_alerts_carry_only_template_words_never_an_address() -> None:
    watch = UptimeWatch(NAMES)
    alerts = watch.observe({"eva": report(a=None, b=401)})
    text = " ".join(f"{a.title} {a.message}" for a in alerts)
    assert "example" not in text
    assert "https" not in text
    assert "did not answer, refused the request" in text


def test_a_wrong_answer_has_its_own_words() -> None:
    rule = SiteRule(url=A, expect="status", status=405, part="api")
    check = check_site(rule, StatusAnswer(status=404, elapsed_ms=50), NOW)
    wrong = UptimeReport(sites=(check,), fetched_at=NOW)
    (alert,) = UptimeWatch(NAMES).observe({"zaaheen": wrong})
    assert "answered as something else" in alert.message
    assert "not as expected" not in alert.message


def test_an_unknown_project_uses_its_id_and_the_threshold_is_checked() -> None:
    (alert,) = UptimeWatch({}).observe({"nearby": report(a=500)})
    assert alert.title == "Agent Zaaheen: nearby is down"
    with pytest.raises(ValueError, match="at least one"):
        UptimeWatch(NAMES, threshold=0)


# ---------------------------------------------------------------- the push client


def push(handler: object) -> PushClient:
    return PushClient(TOPIC, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_a_push_is_one_post_of_plain_text_to_the_secret_topic() -> None:
    seen: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "x"})

    asyncio.run(push(serve).send("Agent Zaaheen: Eva is down", "1 of 2 addresses down.", priority="high"))
    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", f"https://ntfy.sh/{TOPIC.get_secret_value()}")
    assert (request.headers["Title"], request.headers["Priority"]) == ("Agent Zaaheen: Eva is down", "high")
    assert request.content == b"1 of 2 addresses down."


@pytest.mark.parametrize(
    ("title", "message", "priority"),
    [
        ("", "x", "default"),
        ("t", "", "default"),
        ("t", "two\nlines", "default"),
        ("t", "x" * 2000, "default"),
        ("t" * 200, "x", "default"),
        ("t", "x", "urgent"),
    ],
)
def test_odd_messages_are_refused_before_sending(title: str, message: str, priority: str) -> None:
    def serve(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing may be sent")

    with pytest.raises(NetError) as caught:
        asyncio.run(push(serve).send(title, message, priority=priority))
    assert caught.value.net_code is NetErrorCode.BLOCKED


def test_the_briefing_goes_as_one_post_of_several_lines() -> None:
    seen: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    asyncio.run(
        push(serve).send_lines("Agent Zaaheen briefing, Sun 25 Oct", ["2 projects.", "Details here."])
    )
    (request,) = seen
    assert request.headers["Title"] == "Agent Zaaheen briefing, Sun 25 Oct"
    assert request.content == b"2 projects.\nDetails here."


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["a line", ""],
        ["one line\nhides two"],
        ["a tab\tis not printable"],
        ["x" * 1000] * 4,  # over the limit together
    ],
)
def test_odd_briefings_are_refused_before_sending(lines: list[str]) -> None:
    def serve(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing may be sent")

    with pytest.raises(NetError) as caught:
        asyncio.run(push(serve).send_lines("Agent Zaaheen briefing", lines))
    assert caught.value.net_code is NetErrorCode.BLOCKED


@pytest.mark.parametrize("topic", ["short", "has space in it here", "a/b/c/d/e/f/g/h/i/j", "x" * 80])
def test_an_odd_topic_is_refused(topic: str) -> None:
    with pytest.raises(NetError):
        PushClient(SecretStr(topic))


def test_only_the_ntfy_host_is_allowed() -> None:
    with pytest.raises(NetError):
        PushClient(TOPIC, host="evil.example")


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (httpx.Response(429), NetErrorCode.CLIENT_ERROR),
        (httpx.Response(302, headers={"Location": "https://elsewhere.example"}), NetErrorCode.CLIENT_ERROR),
        (httpx.ConnectError("down"), NetErrorCode.UNAVAILABLE),
        (httpx.ReadTimeout("slow"), NetErrorCode.TIMEOUT),
    ],
)
def test_a_failed_send_says_why_and_never_names_the_topic(answer: object, code: NetErrorCode) -> None:
    def serve(_request: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]

    with pytest.raises(NetError) as caught:
        asyncio.run(push(serve).send("t", "x"))
    assert caught.value.net_code is code
    assert TOPIC.get_secret_value() not in str(caught.value)


class Pusher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, title: str, message: str, *, priority: str = "default") -> None:
        if self.fail:
            raise SourceError(SourceErrorCode.UNAVAILABLE, "down")
        self.sent.append((title, message, priority))


def test_deliver_counts_what_went_out_and_never_raises() -> None:
    alerts = UptimeWatch(NAMES).observe({"eva": report(a=404), "zaaheen": report(a=500)})
    assert asyncio.run(deliver(alerts, Pusher())) == 2
    assert asyncio.run(deliver(alerts, Pusher(fail=True))) == 0


# ---------------------------------------------------------------- the re-checking watcher


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def test_each_round_keeps_the_latest_check_and_pushes_only_changes() -> None:
    statuses = {"a.example": 200}
    pushed: list[bytes] = []

    def serve(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            pushed.append(request.content)
            return httpx.Response(200)
        return httpx.Response(statuses[request.url.host])

    clock = Clock()
    live = UptimeLive(
        {"eva": (SiteRule(url=A),)}, NAMES, clock=clock, transport=httpx.MockTransport(serve), topic=TOPIC
    )
    assert asyncio.run(live.round()) == []
    statuses["a.example"] = 503
    clock.now += timedelta(minutes=5)
    assert asyncio.run(live.round()) == []  # one failure: no alert yet
    assert live.latest["eva"].up == 0  # but the card already shows it down
    clock.now += timedelta(minutes=5)
    (alert,) = asyncio.run(live.round())
    assert alert.down
    assert pushed == [b"1 of 1 address down (server error). Details on the dashboard."]
    assert live.latest["eva"].fetched_at == clock.now


def test_without_a_topic_nothing_is_pushed() -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "no push without a topic"
        return httpx.Response(500)

    live = UptimeLive({"eva": (SiteRule(url=A),)}, NAMES, clock=Clock(), transport=httpx.MockTransport(serve))
    (alert,) = asyncio.run(live.round())
    assert alert.down


def test_the_background_thread_rechecks_and_stops() -> None:
    rounds: list[int] = []

    def serve(_request: httpx.Request) -> httpx.Response:
        rounds.append(1)
        return httpx.Response(200)

    live = UptimeLive(
        {"eva": (SiteRule(url=A),)},
        NAMES,
        clock=Clock(),
        transport=httpx.MockTransport(serve),
        interval_s=0.01,
    )
    thread = live.start()
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while len(rounds) < 2 and datetime.now(UTC) < deadline:  # a plain thread, polled briefly
        thread.join(0.01)
    live.stop()
    thread.join(2)
    assert len(rounds) >= 2
    assert not thread.is_alive()


def test_a_failed_round_does_not_end_the_rechecking(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[int] = []

    def serve(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("something odd")
        return httpx.Response(200)

    live = UptimeLive(
        {"eva": (SiteRule(url=A),)},
        NAMES,
        clock=Clock(),
        transport=httpx.MockTransport(serve),
        interval_s=0.01,
    )
    thread = live.start()
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while len(calls) < 2 and datetime.now(UTC) < deadline:
        thread.join(0.01)
    live.stop()
    thread.join(2)
    assert len(calls) >= 2


def test_start_watch_needs_a_usable_topic(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rules = {"eva": (SiteRule(url=A),)}
    assert start_watch(rules, NAMES, {}, "AGENTZAAHEEN_NTFY_TOPIC", clock=Clock()) is None
    assert "AGENTZAAHEEN_NTFY_TOPIC is not set" in capsys.readouterr().out
    assert start_watch(rules, NAMES, {"T": "has space"}, "T", clock=Clock()) is None


def test_start_watch_runs_the_first_round(capsys: pytest.CaptureFixture[str]) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    live = start_watch(
        {"eva": (SiteRule(url=A),)},
        NAMES,
        {"T": TOPIC.get_secret_value()},
        "T",
        clock=Clock(),
        transport=httpx.MockTransport(serve),
    )
    assert live is not None
    assert live.latest["eva"].up == 1
    assert "alerts go to your phone" in capsys.readouterr().out
    assert TOPIC.get_secret_value() not in capsys.readouterr().out


def test_a_round_is_one_trace_holding_every_probe() -> None:
    live = UptimeLive(
        {"eva": (SiteRule(url=A),)},
        NAMES,
        clock=Clock(),
        transport=httpx.MockTransport(lambda _r: httpx.Response(500)),
    )
    with recorded() as spans:
        (alert,) = asyncio.run(live.round())
    assert alert.down
    round_span = spans.named("uptime round")
    assert (round_span.attributes or {})["agentzaaheen.count"] == 1
    probes = [s for s in spans.spans if s.name.startswith("probe ")]
    assert probes
    assert round_span.context is not None
    assert all(p.context is not None and p.context.trace_id == round_span.context.trace_id for p in probes)


@pytest.mark.parametrize(
    ("title", "lines"),
    [
        ("Agent Zaaheen briefing", ["Eva: 1 alert.", "token ghp_" + "Q" * 36]),  # built here, never real
        ("Agent Zaaheen: key sk_live_" + "Q" * 24, ["Eva is down."]),
    ],
)
def test_a_phone_message_holding_a_secret_is_never_sent(title: str, lines: list[str]) -> None:
    def serve(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing may be sent")

    with pytest.raises(NetError) as caught:
        asyncio.run(push(serve).send_lines(title, lines))
    assert caught.value.reason_code == NetErrorCode.BLOCKED.value
    assert "Q" * 24 not in str(caught.value)
