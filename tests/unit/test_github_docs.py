"""The knowledge bank's GitHub reader against a pretend GitHub: only listed documents, never secrets (F12)."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.net import JsonClient, NetPolicy
from agentzaaheen.core.sdk import SourceError, SourceErrorCode
from agentzaaheen.plugins.connectors.github import ALLOWED_HOSTS, API_HEADERS, BASE_URL, DocList, GitHubDocs
from agentzaaheen.plugins.connectors.github.files import MAX_DOC_BYTES, MAX_DOCS

REPO = "owner/shop"
SHA = "c" * 40


class World:
    """A repository on a pretend GitHub: its tree entries and files."""

    def __init__(self) -> None:
        self.tree: list[dict[str, Any]] = [
            {"type": "tree", "path": "site"},
            {"type": "blob", "path": "README.md", "size": 10, "sha": SHA},
            {"type": "blob", "path": "site/index.astro", "size": 10, "sha": SHA},
            {"type": "blob", "path": "site/logo.png", "size": 10, "sha": SHA},
            {"type": "blob", "path": "site/.env.md", "size": 10, "sha": SHA},
            {"type": "blob", "path": "site/huge.md", "size": MAX_DOC_BYTES + 1, "sha": SHA},
            {"type": "blob", "path": "../odd.md", "size": 10, "sha": SHA},
        ]
        self.truncated: object = False
        self.content: dict[str, Any] = {"encoding": "base64", "content": base64.b64encode(b"# Shop").decode()}
        self.status = 200

    def serve(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.status != 200:
            return httpx.Response(self.status)
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == f"/repos/{REPO}/git/trees/main":
            return httpx.Response(200, json={"truncated": self.truncated, "tree": self.tree})
        if path.startswith(f"/repos/{REPO}/contents/"):
            return httpx.Response(200, json=self.content)
        return httpx.Response(404)


def listed(world: World, wanted: list[str]) -> DocList:
    async def body() -> DocList:
        async with JsonClient(
            BASE_URL,
            policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
            headers=API_HEADERS,
            secret_headers={"Authorization": SecretStr("Bearer t")},
            transport=httpx.MockTransport(world.serve),
        ) as http:
            return await GitHubDocs(http, REPO).listed(wanted)

    return asyncio.run(body())


def text(world: World) -> str | None:
    async def body() -> str | None:
        async with JsonClient(
            BASE_URL,
            policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
            headers=API_HEADERS,
            transport=httpx.MockTransport(world.serve),
        ) as http:
            return await GitHubDocs(http, REPO).text("README.md", "main")

    return asyncio.run(body())


def test_only_listed_documents_are_found_never_secrets_pictures_or_huge_files() -> None:
    found = listed(World(), ["README.md", "site/", "gone.md", "../odd.md"])
    assert found.branch == "main"
    assert found.found == {"README.md": SHA, "site/index.astro": SHA}
    assert found.missing == ("gone.md", "../odd.md")


def test_no_more_than_so_many_documents_are_read_for_one_project() -> None:
    world = World()
    world.tree = [
        {"type": "blob", "path": f"docs/{n}.md", "size": 1, "sha": SHA} for n in range(MAX_DOCS + 5)
    ]
    assert len(listed(world, ["docs/"]).found) == MAX_DOCS


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"truncated": True}, "cut short"),
        ({"size": -1}, "file size"),
        ({"size": True}, "file size"),
        ({"sha": "not-an-id"}, "file id"),
        ({"sha": None}, "file id"),
    ],
)
def test_an_odd_tree_is_refused(change: dict[str, object], why: str) -> None:
    world = World()
    if "truncated" in change:
        world.truncated = change["truncated"]
    else:
        world.tree[1] = {**world.tree[1], **change}
    with pytest.raises(SourceError) as caught:
        listed(world, ["README.md"])
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE
    assert why in str(caught.value)


def test_a_tree_that_is_not_a_list_is_refused() -> None:
    world = World()
    world.tree = "nope"  # type: ignore[assignment]
    with pytest.raises(SourceError):
        listed(world, ["README.md"])


def test_github_failing_says_where() -> None:
    world = World()
    world.status = 500
    with pytest.raises(SourceError) as caught:
        listed(world, ["README.md"])
    assert caught.value.operation == f"GET /repos/{REPO}"  # the innermost step is kept


def test_a_documents_text_is_read_and_a_binary_one_is_none() -> None:
    world = World()
    assert text(world) == "# Shop"
    world.content = {"encoding": "base64", "content": base64.b64encode(b"\xff\xfe\x00").decode()}
    assert text(world) is None


def test_a_document_that_cannot_be_read_says_where() -> None:
    world = World()
    world.content = {"encoding": "none"}
    with pytest.raises(SourceError) as caught:
        text(world)
    assert caught.value.operation == "read a github document"
