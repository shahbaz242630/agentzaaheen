"""One suite, two implementations: the fake build-log source and the real one (Rulebook §2.3, ADR-029).

The real ``GitHubBuildLog`` runs through the core's real ``JsonClient`` (with
its download policy) against ``LogWorld``, so the redirect, the host pattern
and the token handling are all exercised. Both must behave the same.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.net import DownloadPolicy, JsonClient, NetPolicy
from agentzaaheen.core.sdk import BuildLogSource, FailedStepLog, SourceError, SourceErrorCode
from agentzaaheen.plugins.connectors.github import (
    ALLOWED_HOSTS,
    API_HEADERS,
    BASE_URL,
    LOG_HOST,
    FakeGitHubBuildLog,
    GitHubBuildLog,
)
from tests.fixtures import github_log_world as world

FAKE_TOKEN = "github_pat_" + "y" * 30  # built here, never a real token


def read(log_world: world.LogWorld) -> FailedStepLog | None:
    async def body() -> FailedStepLog | None:
        async with JsonClient(
            BASE_URL,
            policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
            headers=API_HEADERS,
            secret_headers={"Authorization": SecretStr(f"Bearer {FAKE_TOKEN}")},
            transport=httpx.MockTransport(log_world.serve),
            download=DownloadPolicy(host_pattern=LOG_HOST),
        ) as http:
            source: BuildLogSource = GitHubBuildLog(http, world.REPO)
            return await source.fetch(now=world.NOW)

    return asyncio.run(body())


Fetch = Callable[[bool], FailedStepLog | None]


def _real(failed: bool) -> FailedStepLog | None:
    return read(world.LogWorld(failed_runs=failed))


def _fake(failed: bool) -> FailedStepLog | None:
    source: BuildLogSource = FakeGitHubBuildLog(world.EXPECTED if failed else None, world.REPO)
    return asyncio.run(source.fetch(now=world.NOW))


@pytest.fixture(params=["real", "fake"])
def fetch(request: pytest.FixtureRequest) -> Fetch:
    return _real if request.param == "real" else _fake


# ---------------------------------------------------------------- the shared contract


def test_returns_the_cleaned_log_of_the_failed_job(fetch: Fetch) -> None:
    assert fetch(True) == world.EXPECTED


def test_no_failed_build_means_none(fetch: Fetch) -> None:
    assert fetch(False) is None


def test_the_excerpt_ends_at_the_error_with_the_token_blanked(fetch: Fetch) -> None:
    log = fetch(True)
    assert log is not None
    assert log.lines[-1] == "##[error]Process completed with exit code 1."
    assert "Post job cleanup." not in log.lines
    assert log.truncated is True
    assert world.FAKE_TOKEN_IN_LOG not in "\n".join(log.lines)
    assert "FAIL src/sum.test.ts: expected 3, received 4" in log.lines
    assert world.INJECTION in log.lines  # passed on as data, never acted on here


def test_a_failure_is_a_source_error(fetch: Fetch) -> None:
    if fetch is _fake:
        fake = FakeGitHubBuildLog(world.EXPECTED, world.REPO)
        fake.fail_with = SourceErrorCode.AUTH
        with pytest.raises(SourceError):
            asyncio.run(fake.fetch(now=world.NOW))
        return

    class Broken(world.LogWorld):
        def serve(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/logs"):
                return httpx.Response(401)
            return super().serve(request)

    with pytest.raises(SourceError) as caught:
        read(Broken())
    assert caught.value.code is SourceErrorCode.AUTH
    assert caught.value.operation == f"GET /repos/{world.REPO}/actions/jobs/{world.JOB_ID}/logs (download)"


# ---------------------------------------------------------------- the real one only


def test_the_token_goes_to_github_only() -> None:
    log_world = world.LogWorld()
    read(log_world)
    hosts = [(r.url.host, "Authorization" in r.headers) for r in log_world.seen]
    assert hosts[-1] == (world.LOG_HOST, False)
    assert all(has_token for host, has_token in hosts if host == "api.github.com")


def test_it_asks_for_exactly_the_named_endpoints() -> None:
    log_world = world.LogWorld()
    read(log_world)
    calls = [(r.url.path, dict(r.url.params)) for r in log_world.seen if r.url.host == "api.github.com"]
    assert calls == [
        (f"/repos/{world.REPO}", {}),
        (f"/repos/{world.REPO}/actions/runs", {"branch": "main", "status": "failure", "per_page": "1"}),
        (f"/repos/{world.REPO}/actions/runs/{world.RUN_ID}/jobs", {"filter": "latest", "per_page": "100"}),
        (f"/repos/{world.REPO}/actions/jobs/{world.JOB_ID}/logs", {}),
    ]


def test_a_failed_run_without_a_failed_job_means_none() -> None:
    log_world = world.LogWorld(failed_job=False)
    assert read(log_world) is None
    assert not any(r.url.path.endswith("/logs") for r in log_world.seen)


@pytest.mark.parametrize(
    "host",
    [
        "productionresultssa0.blob.core.windows.net",
        "productionresultssa5.blob.core.windows.net",
        "productionresultssa19.blob.core.windows.net",
        "productionresultssa99.blob.core.windows.net",
    ],
)
def test_the_log_host_pattern_takes_githubs_storage(host: str) -> None:
    assert LOG_HOST.fullmatch(host)


@pytest.mark.parametrize(
    "host",
    [
        "productionresultssa100.blob.core.windows.net",
        "productionresultssa.blob.core.windows.net",
        "productionresultssa5.blob.core.windows.net.evil.com",
        "evilproductionresultssa5.blob.core.windows.net",
        "productionresultssa5.blob.core.windows.org",
        "attacker.blob.core.windows.net",
    ],
)
def test_the_log_host_pattern_refuses_lookalikes(host: str) -> None:
    assert LOG_HOST.fullmatch(host) is None
