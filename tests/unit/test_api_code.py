"""The Code card: counts and one build line while fresh; all unknown once old; why, when a read failed."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.fixtures.github_world import EXPECTED, HEAD_SHA, NOW, when
from yaqzan.core.api.code import code_card, failed_code_card
from yaqzan.core.api.views import Card, Count, NotKnown, Tone
from yaqzan.core.metrics import Freshness
from yaqzan.core.sdk import BuildState, RepoHealth

HOUR = timedelta(hours=1)


def card(health: RepoHealth = EXPECTED, *, later: timedelta = timedelta(0)) -> Card:
    return code_card(health, source_label="GitHub (live)", schedule=HOUR, now=NOW + later)


def test_a_fresh_card_shows_the_counts_and_the_build() -> None:
    shown = card()
    assert (shown.id, shown.title, shown.source, shown.sample) == ("github", "Code", "GitHub (live)", False)
    assert shown.freshness is Freshness.FRESH
    assert shown.last_success_at == NOW
    assert [(m.label, m.value) for m in shown.metrics] == [
        ("Open issues", Count(value=5)),
        ("Open pull requests", Count(value=2)),
        ("Failing workflows", Count(value=0)),
    ]
    assert shown.metrics[2].detail == "main, newest commit"
    (line,) = shown.entries
    assert (line.text, line.tone, line.at) == ("Newest build on main passed (bbbbbbb)", Tone.OK, when(11, 9))


@pytest.mark.parametrize(
    ("changes", "text", "tone"),
    [
        (
            {"build": BuildState.FAILED, "failing_workflows": 1},
            "Newest build on main failed: 1 workflow (bbbbbbb)",
            Tone.ALERT,
        ),
        (
            {"build": BuildState.FAILED, "failing_workflows": 3},
            "Newest build on main failed: 3 workflows (bbbbbbb)",
            Tone.ALERT,
        ),
        (
            {"build": BuildState.RUNNING, "build_finished_at": None},
            "Newest build on main is still running (bbbbbbb)",
            Tone.INFO,
        ),
        (
            {"build": BuildState.NONE, "build_commit": None, "build_finished_at": None},
            "No builds have run on main yet",
            Tone.INFO,
        ),
    ],
)
def test_the_build_line_says_what_happened(changes: dict[str, object], text: str, tone: Tone) -> None:
    health = RepoHealth.model_validate({**EXPECTED.model_dump(), **changes})
    (line,) = card(health).entries
    assert (line.text, line.tone) == (text, tone)
    assert HEAD_SHA[:7] in text or health.build is BuildState.NONE


def test_an_old_read_turns_every_number_unknown_and_drops_the_build_line() -> None:
    shown = card(later=HOUR * 3)
    assert shown.freshness is Freshness.STALE
    assert all(m.value == NotKnown(reason="freshness.stale") for m in shown.metrics)
    assert shown.entries == ()
    assert shown.last_success_at == NOW


def test_a_failed_read_says_why_in_words() -> None:
    shown = failed_code_card("net.auth", source_label="GitHub (live)")
    assert shown.freshness is Freshness.NEVER
    assert shown.last_success_at is None
    assert all(m.value == NotKnown(reason="net.auth") for m in shown.metrics)
    (line,) = shown.entries
    assert line.tone is Tone.WARN
    assert line.text.startswith("GitHub refused our API key.")
    assert len(line.text) <= 160
