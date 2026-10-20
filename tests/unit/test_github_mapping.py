"""How GitHub's workflow runs and search answers become one build state and two counts."""

from __future__ import annotations

import pytest

from tests.fixtures.github_world import HEAD_SHA, OLD_SHA, at, run, when
from yaqzan.core.sdk import BuildState, SourceError, SourceErrorCode
from yaqzan.plugins.connectors.github import mapping


def test_no_runs_means_no_build() -> None:
    assert mapping.build([]) == mapping.Build(BuildState.NONE, None, 0, None)


def test_only_the_newest_commit_counts() -> None:
    built = mapping.build(
        [
            run(2, 101, HEAD_SHA, created=at(11), updated=at(11, 5)),
            run(1, 101, OLD_SHA, created=at(9), updated=at(9, 5), conclusion="failure"),
        ]
    )
    assert built == mapping.Build(BuildState.PASSED, HEAD_SHA, 0, when(11, 5))


def test_a_rerun_of_a_workflow_replaces_its_earlier_run() -> None:
    built = mapping.build(
        [
            run(1, 101, HEAD_SHA, created=at(11), updated=at(11, 5), conclusion="failure"),
            run(2, 101, HEAD_SHA, created=at(11, 30), updated=at(11, 40)),
        ]
    )
    assert built.state is BuildState.PASSED
    assert built.finished_at == when(11, 40)


def test_one_failed_workflow_fails_the_build_even_while_another_runs() -> None:
    built = mapping.build(
        [
            run(1, 101, HEAD_SHA, created=at(11), updated=at(11, 5), conclusion="timed_out"),
            run(2, 102, HEAD_SHA, created=at(11), updated=at(11, 1), status="in_progress", conclusion=None),
            run(3, 103, HEAD_SHA, created=at(11), updated=at(11, 7), conclusion="failure"),
        ]
    )
    assert built == mapping.Build(BuildState.FAILED, HEAD_SHA, 2, when(11, 7))


def test_an_unfinished_workflow_means_running() -> None:
    built = mapping.build(
        [
            run(1, 101, HEAD_SHA, created=at(11), updated=at(11, 5)),
            run(2, 102, HEAD_SHA, created=at(11), updated=at(11, 1), status="queued", conclusion=None),
        ]
    )
    assert built == mapping.Build(BuildState.RUNNING, HEAD_SHA, 0, None)


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "neutral", "stale"])
def test_cancelled_or_skipped_runs_are_not_failures(conclusion: str) -> None:
    built = mapping.build([run(1, 101, HEAD_SHA, created=at(11), updated=at(11, 5), conclusion=conclusion)])
    assert built.state is BuildState.PASSED


@pytest.mark.parametrize(
    ("status", "conclusion", "code"),
    [
        ("completed", "exploded", SourceErrorCode.UNKNOWN_VALUE),
        ("vanished", None, SourceErrorCode.UNKNOWN_VALUE),
        ("completed", None, SourceErrorCode.BAD_RESPONSE),
    ],
)
def test_values_we_do_not_know_are_refused_not_guessed(
    status: str, conclusion: str | None, code: SourceErrorCode
) -> None:
    with pytest.raises(SourceError) as caught:
        mapping.build(
            [run(1, 101, HEAD_SHA, created=at(11), updated=at(11, 5), status=status, conclusion=conclusion)]
        )
    assert caught.value.code is code


@pytest.mark.parametrize(
    "broken",
    [
        {"head_sha": "abc"},
        {"head_sha": "B" * 40},
        {"workflow_id": "101"},
        {"workflow_id": True},
        {"workflow_id": -1},
        {"created_at": "yesterday"},
        {"created_at": "2026-10-20T11:00:00"},
    ],
)
def test_a_broken_run_is_a_bad_response(broken: dict[str, object]) -> None:
    bad = {**run(1, 101, HEAD_SHA, created=at(11), updated=at(11, 5)), **broken}
    with pytest.raises(SourceError) as caught:
        mapping.build([bad])
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        ({"total_count": 0, "incomplete_results": False}, 0),
        ({"total_count": 12, "incomplete_results": False}, 12),
    ],
)
def test_a_complete_search_gives_its_count(page: dict[str, object], expected: int) -> None:
    assert mapping.search_total(page) == expected


@pytest.mark.parametrize(
    ("page", "code"),
    [
        ({"total_count": 3, "incomplete_results": True}, SourceErrorCode.UNKNOWN_VALUE),
        ({"total_count": 3}, SourceErrorCode.UNKNOWN_VALUE),
        ({"total_count": -1, "incomplete_results": False}, SourceErrorCode.BAD_RESPONSE),
        ({"total_count": "3", "incomplete_results": False}, SourceErrorCode.BAD_RESPONSE),
    ],
)
def test_an_unusable_search_count_is_refused(page: dict[str, object], code: SourceErrorCode) -> None:
    with pytest.raises(SourceError) as caught:
        mapping.search_total(page)
    assert caught.value.code is code


@pytest.mark.parametrize("value", [None, "nope", 3])
def test_objects_must_be_json_objects(value: object) -> None:
    with pytest.raises(SourceError):
        mapping.objects([value], "workflow_runs")
