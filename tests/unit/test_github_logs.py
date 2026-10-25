"""The GitHub build-log reader's edges (shared behaviour: tests/contract/test_build_log_contract.py)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.net import DownloadPolicy, JsonClient, NetPolicy
from agentzaaheen.core.sdk import FailedStepLog, SourceError, SourceErrorCode
from agentzaaheen.plugins.connectors.github import (
    ALLOWED_HOSTS,
    API_HEADERS,
    BASE_URL,
    LOG_HOST,
    GitHubBuildLog,
)
from tests.fixtures import github_log_world as world

Serve = Callable[[httpx.Request], httpx.Response]


def read(serve: Serve) -> FailedStepLog | None:
    async def body() -> FailedStepLog | None:
        async with JsonClient(
            BASE_URL,
            policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS, max_attempts=1),
            headers=API_HEADERS,
            secret_headers={"Authorization": SecretStr("Bearer github_pat_" + "w" * 30)},
            transport=httpx.MockTransport(serve),
            download=DownloadPolicy(host_pattern=LOG_HOST),
        ) as http:
            return await GitHubBuildLog(http, world.REPO).fetch(now=world.NOW)

    return asyncio.run(body())


def with_change(path_end: str, change: Callable[[dict[str, object]], dict[str, object]]) -> Serve:
    base = world.LogWorld()

    def serve(request: httpx.Request) -> httpx.Response:
        response = base.serve(request)
        if request.url.path.endswith(path_end) and response.status_code == 200:
            return httpx.Response(200, json=change(response.json()))
        return response

    return serve


@pytest.mark.parametrize("repo", ["no-slash", "o/..", "o/r/x", "o/ r"])
def test_a_bad_repository_name_is_refused(repo: str) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        GitHubBuildLog(object(), repo)  # type: ignore[arg-type]  # never used: refused first


def test_it_says_which_repository_it_reads() -> None:
    assert GitHubBuildLog(object(), world.REPO).repo == world.REPO  # type: ignore[arg-type]  # not called


def test_a_run_numbered_zero_is_refused_before_any_further_call() -> None:
    def zero(page: dict[str, object]) -> dict[str, object]:
        return {**page, "workflow_runs": [{"id": 0, "head_sha": world.HEAD_SHA}]}

    calls: list[str] = []
    serve = with_change("/actions/runs", zero)

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return serve(request)

    with pytest.raises(SourceError) as caught:
        read(counting)
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE
    assert caught.value.operation == "read github build log"
    assert not any(path.endswith("/jobs") for path in calls)  # refused before asking for run 0


def test_data_that_breaks_the_log_shape_is_a_bad_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(**fields: object) -> FailedStepLog:
        return FailedStepLog.model_validate({**fields, "digest": "0" * 64})

    monkeypatch.setattr("agentzaaheen.plugins.connectors.github.logs.make_failed_step_log", broken)
    with pytest.raises(SourceError) as caught:
        read(world.LogWorld().serve)
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE


def test_an_api_error_says_which_call_failed() -> None:
    base = world.LogWorld()

    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs"):
            return httpx.Response(404)
        return base.serve(request)

    with pytest.raises(SourceError) as caught:
        read(serve)
    assert caught.value.operation == f"GET /repos/{world.REPO}/actions/runs/{world.RUN_ID}/jobs"


@pytest.mark.parametrize(
    "steps",
    [
        None,
        "not a list",
        [{"name": "Run npm test", "conclusion": "success"}],
        [{"conclusion": "failure"}],
        ["not an object", {"name": "", "conclusion": "failure"}],
    ],
)
def test_a_missing_or_unnamed_failed_step_is_left_unnamed(steps: object) -> None:
    def change(page: dict[str, object]) -> dict[str, object]:
        jobs = page["jobs"]
        assert isinstance(jobs, list)
        return {**page, "jobs": [{**job, "steps": steps} for job in jobs]}

    log = read(with_change("/jobs", change))
    assert log is not None
    assert log.step is None


def test_a_job_with_no_finish_time_is_still_read() -> None:
    def change(page: dict[str, object]) -> dict[str, object]:
        jobs = page["jobs"]
        assert isinstance(jobs, list)
        return {**page, "jobs": [{**job, "completed_at": None} for job in jobs]}

    log = read(with_change("/jobs", change))
    assert log is not None
    assert log.finished_at is None
