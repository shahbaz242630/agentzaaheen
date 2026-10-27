"""The chat's ``slo_status`` tool: each project's SLOs from the checker's counts, every number from code."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.llm_gateway import Tool
from agentzaaheen.core.metrics import Objective, SliKind
from agentzaaheen.core.policy import Standing
from agentzaaheen.core.registry import Kind, State, load_registry
from agentzaaheen.core.sdk import Severity, UptimeHistory, UptimeHour
from agentzaaheen.plugins.connectors.availability import d1_answer
from tools import chat_tools
from tools.dashboard_reread import Reread
from tools.dashboard_sources import ListNotUsableError
from tools.slo_tool import (
    AVAILABILITY_CONNECTOR,
    CHECK_SLO_SKILL,
    EVERY,
    FRESH,
    AvailabilityReader,
    SloBook,
    TargetStandings,
    availability_reader,
    slo_book,
    slo_portfolio,
    slo_status_tool,
    slo_text,
)

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "agentzaaheen" / "plugins"
NOW = datetime(2026, 10, 27, 16, 20, tzinfo=UTC)
TOP = NOW.replace(minute=0)
ACCOUNT = "a" * 32
DATABASE = "12345678-90ab-cdef-1234-567890abcdef"
CHECKER_DB = f"{ACCOUNT}/{DATABASE}"
KEY = "cf" + "x" * 38  # built here, never a real key
REPOS = {"zaaheen": "acme/zaaheen", "eva": "acme/eva"}
WINDOW = timedelta(days=28)


def objective(project: str, kind: SliKind = SliKind.AVAILABILITY) -> Objective:
    return Objective(
        project=project,
        name=f"{project}-{kind.value}",
        kind=kind,
        target=Fraction(995, 1000) if kind is SliKind.AVAILABILITY else Fraction(95, 100),
        window=WINDOW,
        threshold_ms=800 if kind is SliKind.LATENCY else None,
    )


OBJECTIVES = (objective("eva"), objective("eva", SliKind.LATENCY), objective("zaaheen"))


def history(project: str, good: int, hours: int, fetched_at: datetime = NOW) -> UptimeHistory:
    rows = tuple(
        UptimeHour(project=project, hour=TOP - timedelta(hours=i), total=60, good=good) for i in range(hours)
    )
    return UptimeHistory(since=TOP - WINDOW, hours=rows, fetched_at=fetched_at)


# ---------------------------------------------------------------- the words


def test_a_project_within_target_gets_its_budget_as_share_and_time() -> None:
    text = slo_text("zaaheen", OBJECTIVES, history("zaaheen", 60, 28 * 24), NOW)
    assert text.startswith("Project: zaaheen. Service level objectives (SLOs); every number below")
    line = text.splitlines()[1]
    assert line.startswith("- zaaheen-availability: target 99.50% over 28 days.")
    assert "100.00% of 40,320 checks found it up: within target." in line
    assert "Error budget left: 100.00%, about 3 h 21 min of 3 h 21 min." in line
    assert "measured over" not in line  # the whole window, to the hour
    assert line.endswith("No burn alert from the hourly counts.")


def test_a_project_below_target_says_the_budget_is_spent_and_how_fast() -> None:
    line = slo_text("eva", OBJECTIVES, history("eva", 0, 72), NOW).splitlines()[1]
    assert "(measured over 3.0 days so far)" in line
    assert "0.00% of 4,320 checks found it up: BELOW target." in line
    assert "Error budget: spent; failures ran at 200.0 times what the target allows (3 h 21 min)." in line
    assert line.endswith("Burn alert P2: a slow burn over the last 3 days.")


def test_speed_and_error_objectives_say_not_connected_yet() -> None:
    line = slo_text("eva", OBJECTIVES, history("eva", 60, 5), NOW).splitlines()[2]
    assert line == (
        "- eva-latency: target 95.00% over 28 days. "
        "Not connected yet: the product does not send request timings or errors."
    )


@pytest.mark.parametrize(
    ("counts", "said"),
    [
        (None, "Not connected yet: the checker's counts have not been read."),
        (
            history("eva", 60, 5, fetched_at=NOW - FRESH - timedelta(minutes=1)),
            "Unknown: the newest counts are from",
        ),
        (history("zaaheen", 60, 5), "Unknown: no checks were counted in the window."),  # none for eva
    ],
)
def test_counts_missing_old_or_empty_are_never_fine(counts: UptimeHistory | None, said: str) -> None:
    line = slo_text("eva", OBJECTIVES, counts, NOW).splitlines()[1]
    assert said in line
    assert "within target" not in line


def test_a_project_without_slos_says_so() -> None:
    assert slo_text("sandoq-kin", OBJECTIVES, None, NOW).endswith("\nThis project has no SLOs.")


def test_a_fast_burn_would_be_worded_as_urgent() -> None:
    from tools.slo_tool import _burn  # noqa: PLC0415  # only ever P2 from hourly counts today

    assert _burn(Severity.P1) == "Burn alert P1: spending the budget fast right now."


# ---------------------------------------------------------------- reading the checker's counts


class Cloudflare:
    def __init__(self, *answers: httpx.Response) -> None:
        self.answers = list(answers)
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.answers.pop(0)


def reader(cloudflare: Cloudflare, clock: list[datetime]) -> AvailabilityReader:
    return AvailabilityReader(
        SecretStr(KEY),
        account=ACCOUNT,
        database=DATABASE,
        clock=lambda: clock[0],
        window=WINDOW,
        transport=httpx.MockTransport(cloudflare),
    )


ROW = {"project": "eva", "hour": int(TOP.timestamp()) // 3600, "total": 60, "good": 0}


def test_the_reader_reads_at_most_once_an_hour_with_the_key_as_a_bearer() -> None:
    cloudflare = Cloudflare(
        httpx.Response(200, json=d1_answer([ROW])), httpx.Response(200, json=d1_answer([]))
    )
    clock = [NOW]
    counts = reader(cloudflare, clock)
    asyncio.run(counts.refresh())
    asyncio.run(counts.refresh())  # not due yet: nothing sent
    assert len(cloudflare.seen) == 1
    sent = cloudflare.seen[0]
    assert (sent.method, sent.url.host) == ("POST", "api.cloudflare.com")
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(sent.content)["params"] == [int((NOW - WINDOW).timestamp()) // 3600]
    assert counts.latest is not None
    assert [h.project for h in counts.latest.hours] == ["eva"]
    clock[0] = NOW + EVERY
    asyncio.run(counts.refresh())
    assert len(cloudflare.seen) == 2


def test_a_failed_read_keeps_the_last_one(capsys: pytest.CaptureFixture[str]) -> None:
    cloudflare = Cloudflare(httpx.Response(200, json=d1_answer([ROW])), httpx.Response(403))
    clock = [NOW]
    counts = reader(cloudflare, clock)
    asyncio.run(counts.refresh())
    kept = counts.latest
    clock[0] = NOW + EVERY
    asyncio.run(counts.refresh())
    assert counts.latest is kept
    assert '"event": "availability_read_failed"' in capsys.readouterr().err
    assert KEY not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("key_env", "checker_db", "env", "said"),
    [
        (None, CHECKER_DB, {}, ""),
        ("K", None, {"K": KEY}, ""),
        ("K", "not-ids", {"K": KEY}, "--checker-db must be ACCOUNT_ID/DATABASE_ID"),
        ("K", f"{ACCOUNT}/{'-' * 36}", {"K": KEY}, "--checker-db must be ACCOUNT_ID/DATABASE_ID"),
        ("K", CHECKER_DB, {}, "K is not set"),
        ("K", CHECKER_DB, {"K": "two words"}, "K is not set"),
    ],
)
def test_no_reader_without_a_usable_key_and_database(
    capsys: pytest.CaptureFixture[str],
    key_env: str | None,
    checker_db: str | None,
    env: dict[str, str],
    said: str,
) -> None:
    assert availability_reader(env, key_env, checker_db, clock=lambda: NOW, window=WINDOW) is None
    assert said in capsys.readouterr().out


def test_a_usable_key_and_database_make_a_reader(capsys: pytest.CaptureFixture[str]) -> None:
    found = availability_reader({"K": KEY}, "K", CHECKER_DB, clock=lambda: NOW, window=WINDOW)
    assert isinstance(found, AvailabilityReader)
    out = capsys.readouterr().out
    assert "read-only key" in out
    assert KEY not in out


# ---------------------------------------------------------------- the tool and the SLO list


def run(tool: Tool, choice: str) -> str:
    async def go() -> str:
        return await tool.run(choice)

    return asyncio.run(go())


def test_the_tool_offers_the_projects_with_slos_and_reads_when_asked() -> None:
    cloudflare = Cloudflare(httpx.Response(200, json=d1_answer([ROW])))
    tool = slo_status_tool(OBJECTIVES, reader(cloudflare, [NOW]), lambda: NOW)
    assert tool is not None
    assert (tool.name, tool.choices, tool.trusted) == ("slo_status", ("eva", "zaaheen"), True)
    assert tool.workspace_of is not None
    assert tool.workspace_of("eva") == "eva"
    assert "0.00% of 60 checks found it up" in run(tool, "eva")


def test_without_a_reader_the_tool_says_not_connected() -> None:
    tool = slo_status_tool(OBJECTIVES, None, lambda: NOW)
    assert tool is not None
    assert "Not connected yet: the checker's counts have not been read." in run(tool, "zaaheen")


def test_no_tool_without_slos() -> None:
    assert slo_status_tool((), None, lambda: NOW) is None


SLO_YAML = """\
apiVersion: openslo/v1
kind: SLO
metadata: {name: eva-availability}
spec:
  service: eva
  indicator: {metadata: {name: availability}}
  timeWindow: [{duration: 28d, isRolling: true}]
  budgetingMethod: Occurrences
  objectives: [{target: 0.995}]
"""


def test_the_slo_list_makes_a_book_with_its_reader(tmp_path: Path) -> None:
    path = tmp_path / "slo.yaml"
    path.write_text(SLO_YAML, encoding="utf-8")
    book = slo_book(path, {"eva"}, {"K": KEY}, "K", CHECKER_DB, clock=lambda: NOW)
    assert book is not None
    assert [o.name for o in book.objectives] == ["eva-availability"]
    assert isinstance(book.reader, AvailabilityReader)
    assert slo_book(None, {"eva"}, {}, None, None, clock=lambda: NOW) is None


def test_a_broken_slo_list_stops_the_start(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "slo.yaml"
    path.write_text(SLO_YAML.replace("service: eva", "service: nobody"), encoding="utf-8")
    with pytest.raises(ListNotUsableError):
        slo_book(path, {"eva"}, {}, None, None, clock=lambda: NOW)
    assert "dashboard: slo: eva-availability: names an unknown project" in capsys.readouterr().out


def standing(skill: State, connector: State) -> Standing:
    registry = load_registry(PLUGINS)
    states = {(Kind.SKILL, s.id): State.ON for s in registry.skills}
    states.update({(Kind.CONNECTOR, c.id): State.ON for c in registry.connectors})
    states[(Kind.SKILL, CHECK_SLO_SKILL)] = skill
    states[(Kind.CONNECTOR, AVAILABILITY_CONNECTOR)] = connector
    return Standing(states=states, pending=())


def test_the_chat_gets_the_tool_while_check_slo_is_on_and_counts_while_the_connector_is_on() -> None:
    registry = load_registry(PLUGINS)
    cloudflare = Cloudflare(httpx.Response(200, json=d1_answer([ROW])))
    book = SloBook(OBJECTIVES, reader(cloudflare, [NOW]))

    def slo_tool(state: Standing | None, slo: SloBook | None = book) -> Tool | None:
        found, _ = chat_tools.tools_for(registry, PLUGINS, None, REPOS, lambda: NOW, standing=state, slo=slo)
        return next((t for t in found if t.name == "slo_status"), None)

    assert slo_tool(None, None) is None
    assert slo_tool(None, SloBook((), None)) is None  # a list with no objectives offers nothing
    assert slo_tool(standing(State.OFF, State.ON)) is None
    connector_off = slo_tool(standing(State.ON, State.OFF))
    assert connector_off is not None
    assert "have not been read" in run(connector_off, "eva")
    assert cloudflare.seen == []  # the connector is off: nothing read
    on = slo_tool(standing(State.ON, State.ON))
    assert on is not None
    assert "0.00% of 60 checks found it up" in run(on, "eva")
    assert slo_tool(None) is not None  # without switches: everything is on


# ---------------------------------------------------------------- the card and Monitoring's standing


def test_the_portfolio_reads_at_start_and_shows_a_card_where_a_project_has_a_place(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agentzaaheen.core.api import SamplePortfolio  # noqa: PLC0415  # only these tests need the sample

    cloudflare = Cloudflare(
        httpx.Response(200, json=d1_answer([ROW])), httpx.Response(200, json=d1_answer([]))
    )
    counts = reader(cloudflare, [NOW])
    reads: list[Reread] = []
    book = SloBook((*OBJECTIVES, objective("family-checkin")), counts)  # Nearby has no place for the card
    portfolio = slo_portfolio(SamplePortfolio(NOW), book, counts, clock=lambda: NOW, reads=reads)
    assert len(cloudflare.seen) == 1  # read once at start
    assert "2 projects show their uptime target" in capsys.readouterr().out
    eva = portfolio.project("eva")
    assert eva is not None
    card = next(c for c in eva.cards if c.id == "slo")
    assert (card.sample, card.metrics[0].detail) == (False, "of 60 checks; target 99.50%")
    zaaheen = portfolio.project("zaaheen")
    assert zaaheen is not None
    assert (
        next(c for c in zaaheen.cards if c.id == "slo").metrics[0].value.kind == "unknown"
    )  # no rows for it
    assert [r.source for r in reads] == ["availability"]

    async def reread() -> int:
        return await reads[0].read()

    assert asyncio.run(reread()) == 0  # the hourly re-read always reads, even inside the hour
    assert len(cloudflare.seen) == 2


def test_before_the_first_read_the_card_says_never_read() -> None:
    from agentzaaheen.core.api import SamplePortfolio  # noqa: PLC0415

    counts = reader(Cloudflare(httpx.Response(403)), [NOW])
    portfolio = slo_portfolio(
        SamplePortfolio(NOW), SloBook(OBJECTIVES, counts), counts, clock=lambda: NOW, reads=[]
    )
    eva = portfolio.project("eva")
    assert eva is not None
    card = next(c for c in eva.cards if c.id == "slo")
    assert card.freshness.value == "never"


def test_monitoring_gets_each_projects_standing_from_code() -> None:
    cloudflare = Cloudflare(httpx.Response(200, json=d1_answer([ROW])))
    counts = reader(cloudflare, [NOW])
    targets = TargetStandings(SloBook(OBJECTIVES, counts), counts, clock=lambda: NOW)
    assert targets.listed == {"eva", "zaaheen"}  # the projects with an uptime target
    assert targets.standing("eva") is None  # not read yet
    asyncio.run(targets.refresh())
    eva = targets.standing("eva")
    assert eva is not None
    assert (eva.objective, eva.target_bp, eva.up_bp, eva.checks, eva.read_at) == (
        "eva-availability",
        9950,
        0,
        60,
        NOW,
    )
    assert eva.budget_left_bp is not None
    assert eva.budget_left_bp < 0  # overspent
    zaaheen = targets.standing("zaaheen")
    assert zaaheen is not None
    assert (zaaheen.up_bp, zaaheen.budget_left_bp) == (None, None)  # no checks counted: unknown, never fine
    assert targets.standing("sandoq-kin") is None  # no target


def test_monitorings_standing_is_rounded_down_so_never_better() -> None:
    row = {"project": "zaaheen", "hour": int(TOP.timestamp()) // 3600, "total": 40_000, "good": 39_998}
    counts = reader(Cloudflare(httpx.Response(200, json=d1_answer([row]))), [NOW])
    targets = TargetStandings(SloBook(OBJECTIVES, counts), counts, clock=lambda: NOW)
    asyncio.run(targets.refresh())
    zaaheen = targets.standing("zaaheen")
    assert zaaheen is not None
    assert zaaheen.up_bp == 9_999  # 99.995%, never shown as 100%
