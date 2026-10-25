"""The one signed download (ADR-029): one redirect, a strict host pattern, and never our token.

No real network: every request goes to an ``httpx.MockTransport`` handler.
"""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.net import DownloadPolicy, JsonClient, NetError, NetErrorCode, NetPolicy

API = "api.example.com"
BLOB = re.compile(r"files\d{1,2}\.example\.net")
LINK = "https://files7.example.net/logs/1.txt?sig=abc&se=2026"
FAKE_KEY = "ghp_" + "t" * 36  # built here, never a real key
POLICY = NetPolicy(allowed_hosts=frozenset({API}), total_timeout_s=5)
SMALL = DownloadPolicy(BLOB, max_bytes=100)


class World:
    """GitHub's API answering with a redirect, and the download host serving the file."""

    def __init__(
        self, link: str = LINK, api_status: int = 302, file_status: int = 200, body: bytes = b"log"
    ) -> None:
        self.link = link
        self.api_status = api_status
        self.file_status = file_status
        self.body = body
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.host == API:
            headers = {"Location": self.link} if self.link else {}
            return httpx.Response(self.api_status, headers=headers)
        return httpx.Response(self.file_status, content=self.body)


def fetch(world: World, download: DownloadPolicy | None = SMALL) -> bytes:
    async def go() -> bytes:
        async with JsonClient(
            f"https://{API}",
            policy=POLICY,
            secret_headers={"Authorization": SecretStr(f"Bearer {FAKE_KEY}")},
            transport=httpx.MockTransport(world),
            download=download,
        ) as http:
            return await http.get_redirected_bytes("/repos/o/r/actions/jobs/1/logs", {})

    return asyncio.run(go())


def refused(world: World, **kw: DownloadPolicy | None) -> NetError:
    with pytest.raises(NetError) as caught:
        fetch(world, **kw)
    return caught.value


def test_the_file_is_read_through_one_redirect() -> None:
    world = World(body=b"line one\nline two")
    assert fetch(world) == b"line one\nline two"
    assert [r.url.host for r in world.seen] == [API, "files7.example.net"]


def test_our_token_never_reaches_the_download_host() -> None:
    world = World()
    fetch(world)
    api, download = world.seen
    assert api.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert "Authorization" not in download.headers
    assert FAKE_KEY not in str(download.headers)
    assert str(download.url) == LINK  # the signed link is used exactly as given


@pytest.mark.parametrize(
    "link",
    [
        "https://files7.example.net.evil.com/x",
        "https://xfiles7.example.net/x",
        "https://files123.example.net/x",
        "https://evil.com/files7.example.net",
        "http://files7.example.net/x",
        "https://user:pw@files7.example.net/x",
        "https://files7.example.net:8443/x",
        "https://10.0.0.1/x",
        "/relative/path",
    ],
)
def test_a_link_anywhere_else_is_refused_before_any_request(link: str) -> None:
    world = World(link=link)
    error = refused(world)
    assert error.net_code is NetErrorCode.BLOCKED
    assert [r.url.host for r in world.seen] == [API]
    assert "evil" not in str(error)
    assert "pw" not in str(error)


@pytest.mark.parametrize(
    ("world", "code"),
    [
        (World(api_status=200), NetErrorCode.BAD_RESPONSE),
        (World(link=""), NetErrorCode.BAD_RESPONSE),
        (World(api_status=401), NetErrorCode.AUTH),
        (World(api_status=404), NetErrorCode.CLIENT_ERROR),
        (World(file_status=403), NetErrorCode.AUTH),
        (World(file_status=302), NetErrorCode.BLOCKED),
        (World(body=b"x" * 101), NetErrorCode.TOO_LARGE),
    ],
)
def test_every_other_answer_is_a_clear_failure(world: World, code: NetErrorCode) -> None:
    error = refused(world)
    assert error.net_code is code
    assert error.operation == "GET /repos/o/r/actions/jobs/1/logs (download)"


def test_a_client_without_a_download_policy_refuses() -> None:
    world = World()
    assert refused(world, download=None).net_code is NetErrorCode.BLOCKED
    assert world.seen == []


def test_an_odd_path_is_refused_without_echoing_it() -> None:
    async def go() -> None:
        async with JsonClient(
            f"https://{API}",
            policy=POLICY,
            transport=httpx.MockTransport(World()),
            download=DownloadPolicy(BLOB),
        ) as http:
            await http.get_redirected_bytes("/a/../b?x=1", {})

    with pytest.raises(NetError) as caught:
        asyncio.run(go())
    assert caught.value.operation == "GET (refused path)"


@pytest.mark.parametrize("where", [API, "files7.example.net"])
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (httpx.ReadTimeout("slow"), NetErrorCode.TIMEOUT),
        (httpx.ConnectError("down"), NetErrorCode.UNAVAILABLE),
    ],
)
def test_network_failures_on_either_host_are_named(
    where: str, failure: Exception, code: NetErrorCode
) -> None:
    inner = World()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == where:
            raise failure
        return inner(request)

    assert refused(handler).net_code is code  # type: ignore[arg-type]  # a plain handler works as a world


def test_a_download_that_never_ends_is_cut_off() -> None:
    async def go() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == API:
                return httpx.Response(302, headers={"Location": LINK})
            await asyncio.sleep(5)
            return httpx.Response(200, content=b"late")  # pragma: no cover - cut off first

        async with JsonClient(
            f"https://{API}",
            policy=POLICY,
            transport=httpx.MockTransport(handler),
            download=DownloadPolicy(BLOB, timeout_s=0.05),
        ) as http:
            await http.get_redirected_bytes("/x", {})

    with pytest.raises(NetError) as caught:
        asyncio.run(go())
    assert caught.value.net_code is NetErrorCode.TIMEOUT


def test_an_api_that_never_answers_is_cut_off_at_the_total_limit() -> None:
    async def go() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(302, headers={"Location": LINK})  # pragma: no cover - cut off first

        async with JsonClient(
            f"https://{API}",
            policy=NetPolicy(allowed_hosts=frozenset({API}), total_timeout_s=0.05),
            transport=httpx.MockTransport(handler),
            download=DownloadPolicy(BLOB),
        ) as http:
            await http.get_redirected_bytes("/x", {})

    with pytest.raises(NetError) as caught:
        asyncio.run(go())
    assert caught.value.net_code is NetErrorCode.TIMEOUT
    assert caught.value.operation == "GET /x (download)"
