"""The Healthchecks connector: one GET, only slug, status and last ping kept (F11 slice 5a)."""

from __future__ import annotations

import asyncio
import pathlib
from datetime import UTC, datetime

import pytest
import yaml

from agentzaaheen.core.sdk import CheckStatus, HeartbeatCheck, SourceError, SourceErrorCode
from agentzaaheen.plugins.connectors.healthchecks import CHECKS_PATH, FakeHealthchecksApi, HealthchecksSource

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


def fetch(
    answer: dict[str, object] | SourceErrorCode,
) -> tuple[tuple[HeartbeatCheck, ...], FakeHealthchecksApi]:
    api = FakeHealthchecksApi(answer)
    report = asyncio.run(HealthchecksSource(api).fetch(now=NOW))
    assert report.fetched_at == NOW
    return report.checks, api


def test_each_check_keeps_only_its_slug_status_and_last_ping() -> None:
    checks, api = fetch(
        {
            "checks": [
                {
                    "slug": "zaaheen",
                    "name": "Zaaheen (outside checker)",
                    "desc": "free text never read",
                    "status": "down",
                    "last_ping": "2026-10-27T08:02:27+00:00",
                    "unique_key": "abc",
                },
                {"slug": "eva", "status": "new", "last_ping": None},
            ]
        }
    )
    assert checks == (
        HeartbeatCheck(
            slug="zaaheen", status=CheckStatus.DOWN, last_ping=datetime(2026, 10, 27, 8, 2, 27, tzinfo=UTC)
        ),
        HeartbeatCheck(slug="eva", status=CheckStatus.NEW, last_ping=None),
    )
    assert api.asked == [(CHECKS_PATH, {})]


@pytest.mark.parametrize(
    "item",
    [
        {"slug": "", "status": "up"},  # a check with no slug
        {"slug": "Not A Slug", "status": "up"},
        {"slug": "x" * 65, "status": "up"},
        {"slug": "zaaheen", "status": "started-and-something-new"},  # a status this code does not know
        {"status": "up"},
        "not an object",
    ],
)
def test_a_check_we_cannot_judge_is_left_out_so_monitoring_calls_it_missing(item: object) -> None:
    checks, _ = fetch({"checks": [item, {"slug": "eva", "status": "up"}]})
    assert [c.slug for c in checks] == ["eva"]


@pytest.mark.parametrize("when", ["2026-10-27T08:02:27", "yesterday", 12345])
def test_a_ping_time_without_a_zone_or_not_a_time_is_dropped(when: object) -> None:
    checks, _ = fetch({"checks": [{"slug": "eva", "status": "up", "last_ping": when}]})
    assert checks[0].last_ping is None


def test_an_answer_without_a_list_of_checks_is_a_bad_response() -> None:
    with pytest.raises(SourceError) as caught:
        fetch({"checks": "nope"})
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE


def test_more_checks_than_one_project_should_have_is_refused() -> None:
    with pytest.raises(SourceError) as caught:
        fetch({"checks": [{"slug": f"c{i}", "status": "up"} for i in range(201)]})
    assert caught.value.code is SourceErrorCode.TOO_MUCH_DATA


def test_a_refused_key_travels_as_its_code() -> None:
    with pytest.raises(SourceError) as caught:
        fetch(SourceErrorCode.AUTH)
    assert caught.value.code is SourceErrorCode.AUTH


def test_the_manifest_names_the_one_host_and_endpoint_read_only() -> None:
    import agentzaaheen.plugins.connectors.healthchecks as plugin  # noqa: PLC0415  # the folder it lives in

    manifest = yaml.safe_load(
        pathlib.Path(plugin.__file__).with_name("plugin.yaml").read_text(encoding="utf-8")
    )
    assert manifest["permissions"] == ["read"]
    assert manifest["egress"] == ["healthchecks.io"]
    assert manifest["endpoints"] == [f"GET {CHECKS_PATH}"]
