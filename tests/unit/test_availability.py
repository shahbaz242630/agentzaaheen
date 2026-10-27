"""The availability connector: the checker's hourly counts per project, from one fixed query (SLO slice 1).

No real network: ``FakeD1`` stands in for Cloudflare's D1 query API.
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from pydantic import ValidationError

from agentzaaheen.core.sdk import SourceError, SourceErrorCode, UptimeHistory, UptimeHour
from agentzaaheen.plugins.connectors.availability import (
    ALLOWED_HOSTS,
    HOURS_QUERY,
    READ_QUERIES,
    AvailabilitySource,
    FakeD1,
    d1_answer,
    query_path,
)

ACCOUNT = "a" * 32
DATABASE = "12345678-90ab-cdef-1234-567890abcdef"
NOW = datetime(2026, 10, 27, 16, 20, tzinfo=UTC)
HOUR = 491_000  # an hour number: seconds since 1970 divided by 3600


def read(
    answer: dict[str, object] | SourceErrorCode, since: datetime = NOW - timedelta(days=28)
) -> UptimeHistory:
    fake = FakeD1(answer)
    return asyncio.run(
        AvailabilitySource(fake, account=ACCOUNT, database=DATABASE).fetch(since=since, now=NOW)
    )


def row(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {"project": "zaaheen", "hour": HOUR, "total": 60, "good": 59}
    base.update(changes)
    return base


def test_each_row_becomes_one_project_hour_with_its_counts() -> None:
    history = read(d1_answer([row(), row(project="eva", good=0)]))
    start = datetime.fromtimestamp(HOUR * 3600, UTC)
    assert history.hours == (
        UptimeHour(project="zaaheen", hour=start, total=60, good=59),
        UptimeHour(project="eva", hour=start, total=60, good=0),
    )
    assert (history.source, history.fetched_at) == ("checker", NOW)


def test_only_the_fixed_query_is_sent_from_the_start_of_the_since_hour() -> None:
    fake = FakeD1(d1_answer([]))
    since = datetime(2026, 9, 29, 16, 20, tzinfo=UTC)
    history = asyncio.run(
        AvailabilitySource(fake, account=ACCOUNT, database=DATABASE).fetch(since=since, now=NOW)
    )
    path, sql, params = fake.asked[0]
    assert path == f"/client/v4/accounts/{ACCOUNT}/d1/database/{DATABASE}/query"
    assert (sql, params) == (HOURS_QUERY, [int(since.timestamp()) // 3600])
    assert history.since == datetime(2026, 9, 29, 16, 0, tzinfo=UTC)
    assert history.hours == ()  # a quiet checker reads as empty, which the SLO numbers call unknown


def test_the_query_is_a_plain_select_the_core_will_send() -> None:
    assert {HOURS_QUERY} == READ_QUERIES
    assert HOURS_QUERY.startswith("SELECT ")
    assert {"api.cloudflare.com"} == ALLOWED_HOSTS


@pytest.mark.parametrize(
    ("account", "database"),
    [("A" * 32, DATABASE), (ACCOUNT, "x/../../other"), (ACCOUNT + "/x", DATABASE), ("", DATABASE)],
)
def test_ids_that_are_not_cloudflare_ids_are_refused(account: str, database: str) -> None:
    with pytest.raises(ValueError, match="hex ids"):
        query_path(account, database)


@pytest.mark.parametrize(
    "answer",
    [
        {"success": False, "errors": [{"code": 7500}], "result": []},
        {"success": True, "result": []},  # no statement
        {"success": True, "result": [{"success": True, "results": []}, {"success": True, "results": []}]},
        {"success": True, "result": ["not a record"]},
        {"success": True, "result": [{"success": False, "results": []}]},
        {"success": True, "result": [{"success": True}]},  # no rows
        d1_answer(["not a record"]),  # type: ignore[list-item]  # a row that is not a record
        d1_answer([row(hour=True)]),
        d1_answer([row(hour=-1)]),
        d1_answer([row(hour="491000")]),
        d1_answer([row(hour=10**12)]),  # past the year 9999
        d1_answer([row(good=61)]),  # more good than checks
        d1_answer([row(total=0, good=0)]),
        d1_answer([row(project="Not A Project")]),
        d1_answer([row(good=None)]),
    ],
)
def test_an_answer_that_is_not_exactly_what_the_checker_writes_fails_the_whole_read(
    answer: dict[str, object],
) -> None:
    with pytest.raises(SourceError) as caught:
        read(answer)
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE


def test_more_rows_than_one_read_should_have_are_refused() -> None:
    with pytest.raises(SourceError) as caught:
        read(d1_answer([row(hour=HOUR + i) for i in range(20_001)]))
    assert caught.value.code is SourceErrorCode.TOO_MUCH_DATA


def test_a_failed_query_stays_a_source_error() -> None:
    with pytest.raises(SourceError) as caught:
        read(SourceErrorCode.AUTH)
    assert caught.value.code is SourceErrorCode.AUTH


def test_an_hour_always_starts_on_the_hour() -> None:
    with pytest.raises(ValidationError, match="on the hour"):
        UptimeHour(project="eva", hour=NOW, total=1, good=1)


def test_the_source_names_itself() -> None:
    assert (
        AvailabilitySource(FakeD1(d1_answer([])), account=ACCOUNT, database=DATABASE).source == "availability"
    )


def test_the_manifest_only_reads_by_a_named_query_with_one_key() -> None:
    import agentzaaheen.plugins.connectors.availability as plugin  # noqa: PLC0415  # the folder it lives in

    manifest = yaml.safe_load(
        pathlib.Path(plugin.__file__).with_name("plugin.yaml").read_text(encoding="utf-8")
    )
    assert manifest["permissions"] == ["read"]
    assert [c["name"] for c in manifest["credentials"]] == ["key"]
    assert set(manifest["egress"]) == ALLOWED_HOSTS
    assert all(endpoint.startswith("QUERY /") for endpoint in manifest["endpoints"])
