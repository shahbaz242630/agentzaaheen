"""Healthchecks on the dashboard: the local list, and one read-only read per round (F11 slice 5a)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.sdk import CheckStatus
from tools.dashboard_healthchecks import (
    HealthchecksListError,
    HealthchecksReader,
    healthchecks_list,
    healthchecks_reader,
    load_healthchecks,
)
from tools.dashboard_sources import ListNotUsableError

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
PROJECTS = {"zaaheen", "eva"}
KEY = "read-only-key-for-tests"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "healthchecks.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_list_maps_each_project_to_its_check_slugs(tmp_path: Path) -> None:
    listed = load_healthchecks(
        write(tmp_path, "zaaheen: [zaaheen, zaaheen-workers, zaaheen]\neva: [eva]\n"), PROJECTS
    )
    assert listed == {"zaaheen": ("zaaheen", "zaaheen-workers"), "eva": ("eva",)}  # a repeat kept once


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("", "must map each project"),
        ("- zaaheen\n", "must map each project"),
        ("nearby: [nearby]\n", "unknown project"),
        ("eva: []\n", "give a list of 1 to 20"),
        ("eva: eva\n", "give a list of 1 to 20"),
        (f"eva: [{', '.join(f'c{i}' for i in range(21))}]\n", "give a list of 1 to 20"),
        ("eva: [Eva Check]\n", "is not a check slug"),
        (f"eva: [{'x' * 65}]\n", "is not a check slug"),
        ("eva: [1]\n", "is not a check slug"),
        ("eva: [eva\n", "not valid YAML"),
    ],
)
def test_a_broken_list_says_why(tmp_path: Path, text: str, said: str) -> None:
    with pytest.raises(HealthchecksListError, match=said):
        load_healthchecks(write(tmp_path, text), PROJECTS)


def test_a_list_too_big_or_missing_says_why(tmp_path: Path) -> None:
    with pytest.raises(HealthchecksListError, match="too big"):
        load_healthchecks(write(tmp_path, "#" * (16 * 1024 + 1)), PROJECTS)
    with pytest.raises(HealthchecksListError, match="could not be read"):
        load_healthchecks(tmp_path / "missing.yaml", PROJECTS)


def test_no_list_is_empty_and_a_broken_one_stops_the_start_after_saying_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert healthchecks_list(None, PROJECTS) == {}
    with pytest.raises(ListNotUsableError):
        healthchecks_list(write(tmp_path, "nearby: [nearby]\n"), PROJECTS)
    assert "unknown project" in capsys.readouterr().out


class Healthchecks:
    """A pretend healthchecks.io: records what was asked and answers as told."""

    def __init__(self, status: int = 200, body: object | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"checks": [{"slug": "eva", "status": "down"}]}
        self.asked: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(request)
        return httpx.Response(self.status, json=self.body)


def reader(api: Healthchecks) -> HealthchecksReader:
    return HealthchecksReader(SecretStr(KEY), clock=lambda: NOW, transport=httpx.MockTransport(api))


def test_one_get_with_the_key_in_its_header_keeps_the_newest_read() -> None:
    api = Healthchecks()
    hc = reader(api)
    asyncio.run(hc.refresh())
    assert hc.latest is not None
    assert [(c.slug, c.status) for c in hc.latest.checks] == [("eva", CheckStatus.DOWN)]
    (asked,) = api.asked
    assert (asked.method, str(asked.url)) == ("GET", "https://healthchecks.io/api/v3/checks/")
    assert asked.headers["X-Api-Key"] == KEY
    assert KEY not in str(asked.url)


def test_a_failed_read_keeps_the_last_good_one() -> None:
    api = Healthchecks()
    hc = reader(api)
    asyncio.run(hc.refresh())
    first = hc.latest
    api.status = 401
    asyncio.run(hc.refresh())
    assert hc.latest is first  # it turns "not read" by its age, never "fine"


def test_a_failed_first_read_leaves_nothing_read() -> None:
    hc = reader(Healthchecks(status=401))
    asyncio.run(hc.refresh())
    assert hc.latest is None


def test_the_reader_needs_a_usable_key_and_never_prints_it(capsys: pytest.CaptureFixture[str]) -> None:
    assert healthchecks_reader({}, None, clock=lambda: NOW) is None
    assert healthchecks_reader({"HC": " "}, "HC", clock=lambda: NOW) is None
    assert healthchecks_reader({"HC": "two words"}, "HC", clock=lambda: NOW) is None
    assert isinstance(healthchecks_reader({"HC": KEY}, "HC", clock=lambda: NOW), HealthchecksReader)
    out = capsys.readouterr().out
    assert "HC is not set" in out
    assert "reads Healthchecks" in out
    assert KEY not in out
