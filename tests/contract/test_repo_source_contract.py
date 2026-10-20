"""One suite, two implementations: the fake GitHub source and the real one (Rulebook §2.3, §5.4).

The real ``GitHubSource`` runs through the core's real ``JsonClient`` against
``GitHubWorld`` (a pretend GitHub on ``httpx.MockTransport``), so the host
allowlist, the headers and the token are all exercised. The fake is loaded
with what that world must map to. Both must behave the same.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from tests.fixtures import github_world as world
from yaqzan.core.net import JsonClient, NetPolicy
from yaqzan.core.sdk import RepoHealth, RepoSource, SourceError, SourceErrorCode
from yaqzan.plugins.connectors.github import (
    ALLOWED_HOSTS,
    API_HEADERS,
    BASE_URL,
    FakeGitHubSource,
    GitHubSource,
)

FAKE_TOKEN = "github_pat_" + "z" * 30  # built here, never a real token


def client(serve: Callable[[httpx.Request], httpx.Response]) -> JsonClient:
    return JsonClient(
        BASE_URL,
        policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
        headers=API_HEADERS,
        secret_headers={"Authorization": SecretStr(f"Bearer {FAKE_TOKEN}")},
        transport=httpx.MockTransport(serve),
    )


def read(serve: Callable[[httpx.Request], httpx.Response], repo: str = world.REPO) -> RepoHealth:
    async def body() -> RepoHealth:
        async with client(serve) as http:
            return await GitHubSource(http, repo).fetch(now=world.NOW)

    return asyncio.run(body())


Fetch = Callable[[], RepoHealth]


def _real_fetch() -> RepoHealth:
    return read(world.GitHubWorld().serve)


def _fake_fetch() -> RepoHealth:
    return asyncio.run(FakeGitHubSource(world.EXPECTED).fetch(now=world.NOW))


@pytest.fixture(params=["real", "fake"])
def fetch(request: pytest.FixtureRequest) -> Fetch:
    return _real_fetch if request.param == "real" else _fake_fetch


# ---------------------------------------------------------------- the shared contract


def test_returns_the_repository_health(fetch: Fetch) -> None:
    assert fetch() == world.EXPECTED


def test_fetching_twice_gives_the_same_answer(fetch: Fetch) -> None:
    assert fetch() == fetch()


def test_nothing_people_wrote_survives_mapping(fetch: Fetch) -> None:
    dumped = fetch().model_dump_json()
    for never in (world.NEVER_READ, "Never Read", "never-read"):
        assert never not in dumped


def test_both_implement_the_protocol() -> None:
    sources: list[RepoSource] = [
        GitHubSource(client(world.GitHubWorld().serve), world.REPO),
        FakeGitHubSource(world.EXPECTED),
    ]
    assert {(s.source, s.repo) for s in sources} == {("github", world.REPO)}


# ---------------------------------------------------------------- the real adapter only


def test_real_source_asks_only_its_named_get_endpoints() -> None:
    github = world.GitHubWorld()
    read(github.serve)
    assert {r.method for r in github.requests} == {"GET"}
    assert {r.url.host for r in github.requests} == {"api.github.com"}
    assert [r.url.path for r in github.requests] == [
        f"/repos/{world.REPO}",
        "/search/issues",
        "/search/issues",
        f"/repos/{world.REPO}/actions/runs",
    ]
    searches = [r.url.params["q"] for r in github.requests if r.url.path == "/search/issues"]
    assert searches == [f"repo:{world.REPO} is:issue is:open", f"repo:{world.REPO} is:pr is:open"]
    assert all(r.url.params.get("per_page") == "1" for r in github.requests if r.url.path == "/search/issues")
    assert all(r.headers["Authorization"] == f"Bearer {FAKE_TOKEN}" for r in github.requests)
    assert all(r.headers["X-GitHub-Api-Version"] == "2022-11-28" for r in github.requests)
    assert all(r.headers["Accept"] == "application/vnd.github+json" for r in github.requests)


def test_an_incomplete_count_is_refused_not_shown() -> None:
    with pytest.raises(SourceError) as caught:
        read(world.GitHubWorld(incomplete=True).serve)
    assert caught.value.code is SourceErrorCode.UNKNOWN_VALUE
    assert caught.value.operation == "read github data"


def test_a_repository_with_no_builds_says_so() -> None:
    health = read(world.GitHubWorld(runs=[]).serve)
    assert (health.build, health.build_commit, health.build_finished_at) == ("none", None, None)


def test_a_refused_token_surfaces_as_auth_with_where_it_failed() -> None:
    with pytest.raises(SourceError) as caught:
        read(lambda r: httpx.Response(401))
    assert caught.value.code is SourceErrorCode.AUTH
    assert caught.value.reason_code == "net.auth"
    where = (caught.value.operation, caught.value.attempt, caught.value.status)
    assert where == (f"GET /repos/{world.REPO}", 1, 401)
    assert FAKE_TOKEN not in str(caught.value)


def test_a_repository_the_token_cannot_see_is_refused() -> None:
    with pytest.raises(SourceError) as caught:
        read(world.GitHubWorld().serve, repo="acme-labs/other")
    assert caught.value.code is SourceErrorCode.REFUSED
    assert caught.value.status == 404


@pytest.mark.parametrize(
    "body",
    [
        {"default_branch": ""},
        {"default_branch": "main; rm -rf /"},
        {"default_branch": 7},
    ],
)
def test_an_unusable_repository_answer_is_a_bad_response(body: dict[str, object]) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/repos/{world.REPO}":
            return httpx.Response(200, json=body)
        return world.GitHubWorld().serve(request)

    with pytest.raises(SourceError) as caught:
        read(serve)
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE


def test_workflow_runs_that_are_not_a_list_are_a_bad_response() -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": "nope"})
        return world.GitHubWorld().serve(request)

    with pytest.raises(SourceError) as caught:
        read(serve)
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE
    assert caught.value.operation == "read github data"


@pytest.mark.parametrize(
    "repo", ["widget", "acme-labs/", "-acme/widget", "acme/widget/x", "acme/..", "acme/."]
)
def test_only_owner_slash_name_is_accepted(repo: str) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        GitHubSource(client(world.GitHubWorld().serve), repo)


def test_the_fake_can_be_told_to_fail() -> None:
    fake = FakeGitHubSource(world.EXPECTED)
    fake.fail_with = SourceErrorCode.UNAVAILABLE
    with pytest.raises(SourceError) as caught:
        asyncio.run(fake.fetch(now=world.NOW))
    assert caught.value.code is SourceErrorCode.UNAVAILABLE
    assert fake.calls == 1
