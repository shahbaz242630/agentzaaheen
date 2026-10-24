"""What a project is built with, read from a pretend GitHub, and kept until it changes (F17 slice 5b-1)."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from tools.dashboard_profile import LaptopProfiles
from yaqzan.core.net import JsonClient, NetPolicy
from yaqzan.core.sdk import MAX_CODE_BYTES, MAX_MANIFEST_BYTES, RepoProfile, SourceError, SourceErrorCode
from yaqzan.core.store import WorkspaceRow
from yaqzan.plugins.connectors.github import ALLOWED_HOSTS, API_HEADERS, BASE_URL, GitHubProfile, Head

REPO = "owner/shop"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
PUSHED = "2026-10-24T08:00:00Z"
SHOP = WorkspaceRow(uuid4(), "shop", "Shop")
TOKEN = SecretStr("github_pat_" + "w" * 30)


class World:
    """A repository on a pretend GitHub. ``seen`` is every path asked for."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {
            "package.json": json.dumps({"dependencies": {"stripe": "1", "@supabase/supabase-js": "2"}}),
            ".env": "STRIPE_SECRET=sk_live_x",
            "secrets/package.json": json.dumps({"dependencies": {"openai": "1"}}),
            "wrangler.jsonc": "{}",
            "src/app.ts": "export {}",
            "src/api/db.ts": 'const url = "https://abcd.supabase.co/rest/v1"',
            "test/api.ts": 'fetch("https://api.openai.com/v1")',
        }
        self.languages: dict[str, object] = {"TypeScript": 900, "CSS": 100}
        self.pushed: str | None = PUSHED
        self.branch = "main"
        self.sizes: dict[str, int] = {}
        self.truncated = False
        self.seen: list[str] = []

    def serve(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.seen.append(path)
        if request.method != "GET":
            return httpx.Response(405)
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": self.branch, "pushed_at": self.pushed})
        if path == f"/repos/{REPO}/languages":
            return httpx.Response(200, json=self.languages)
        if path == f"/repos/{REPO}/git/trees/{self.branch}" and request.url.params.get("recursive") == "1":
            tree = [
                {"path": p, "type": "blob", "size": self.sizes.get(p, len(t))} for p, t in self.files.items()
            ]
            return httpx.Response(200, json={"tree": tree, "truncated": self.truncated})
        prefix = f"/repos/{REPO}/contents/"
        if path.startswith(prefix) and request.url.params.get("ref") == self.branch:
            content = base64.b64encode(self.files[path.removeprefix(prefix)].encode()).decode()
            return httpx.Response(200, json={"encoding": "base64", "content": content})
        return httpx.Response(404)

    def contents(self) -> list[str]:
        return [p.rsplit("/contents/", 1)[1] for p in self.seen if "/contents/" in p]


def _client(world: World) -> JsonClient:
    return JsonClient(
        BASE_URL,
        policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS, max_attempts=1),
        headers=API_HEADERS,
        secret_headers={"Authorization": TOKEN},
        transport=httpx.MockTransport(world.serve),
    )


def read(world: World) -> RepoProfile:
    async def body() -> RepoProfile:
        async with _client(world) as http:
            reader = GitHubProfile(http, REPO)
            return await reader.profile(await reader.head(), now=NOW)

    return asyncio.run(body())


def test_it_reads_languages_and_the_providers_code_finds() -> None:
    world = World()
    profile = read(world)
    assert [(lang.name, lang.bytes) for lang in profile.languages] == [("TypeScript", 900), ("CSS", 100)]
    assert profile.shares() == (9000, 1000)
    assert [p.id for p in profile.providers] == ["cloudflare", "stripe", "supabase"]
    supabase = next(p for p in profile.providers if p.id == "supabase")
    assert supabase.evidence == ("package.json: @supabase/supabase-js", "src/api/db.ts: supabase.co")
    assert profile.changed_at == datetime(2026, 10, 24, 8, 0, tzinfo=UTC)
    assert (profile.branch, profile.files_read, profile.read_at) == ("main", 3, NOW)


def test_secret_files_are_never_asked_for() -> None:
    world = World()
    profile = read(world)
    assert world.contents() == ["package.json", "wrangler.jsonc", "src/api/db.ts"]
    assert "test/api.ts" not in world.contents()  # tests are not searched for addresses
    assert "openai" not in [p.id for p in profile.providers]


def test_large_manifests_are_not_read() -> None:
    world = World()
    world.sizes = {"package.json": MAX_MANIFEST_BYTES + 1, "src/api/db.ts": MAX_CODE_BYTES + 1}
    profile = read(world)
    assert world.contents() == ["wrangler.jsonc"]
    assert [p.id for p in profile.providers] == ["cloudflare"]


def test_languages_come_largest_first_whatever_order_github_sends() -> None:
    world = World()
    world.languages = {"CSS": 5, "Go": 50, "Astro": 50}
    assert [lang.name for lang in read(world).languages] == ["Astro", "Go", "CSS"]


@pytest.mark.parametrize(
    "languages",
    [{"Go": -1}, {"Go": True}, {"Go": "12"}, {"<script>": 1}],
    ids=["negative", "bool", "text", "name"],
)
def test_a_bad_language_count_is_refused_not_guessed(languages: dict[str, object]) -> None:
    world = World()
    world.languages = languages
    with pytest.raises(SourceError) as caught:
        read(world)
    assert caught.value.code is SourceErrorCode.BAD_RESPONSE


def test_a_tree_cut_short_is_refused_before_any_file_is_read() -> None:
    world = World()
    world.truncated = True
    with pytest.raises(SourceError):
        read(world)
    assert world.contents() == []


def test_a_repository_never_pushed_has_no_change_time() -> None:
    world = World()
    world.pushed = None
    assert read(world).changed_at is None


def test_a_bad_push_time_is_refused() -> None:
    world = World()
    world.pushed = "yesterday"
    with pytest.raises(SourceError):
        read(world)


def test_the_reader_only_uses_get() -> None:
    world = World()
    read(world)
    allowed = {f"/repos/{REPO}", f"/repos/{REPO}/languages", f"/repos/{REPO}/git/trees/main"}
    assert all(p in allowed or p.startswith(f"/repos/{REPO}/contents/") for p in world.seen)


# ---- the laptop's memory: read again only when the repository changed


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def profiles(world: World, clock: Clock, repos: dict[str, str] | None = None) -> LaptopProfiles:
    async def known() -> dict[str, str]:
        return {"shop": REPO} if repos is None else repos

    return LaptopProfiles(TOKEN, known, clock, httpx.MockTransport(world.serve))


def test_a_second_open_with_no_change_costs_one_call_and_says_when_it_checked() -> None:
    world, clock = World(), Clock()
    laptop = profiles(world, clock)
    first = asyncio.run(laptop.profile(SHOP))
    calls = len(world.seen)
    clock.now = NOW + timedelta(minutes=5)
    second = asyncio.run(laptop.profile(SHOP))
    assert first is not None
    assert second is not None
    assert world.seen[calls:] == [f"/repos/{REPO}"]
    assert second.profile is first.profile
    assert second.checked_at == NOW + timedelta(minutes=5)


def test_a_push_means_the_next_open_reads_it_all_again() -> None:
    world, clock = World(), Clock()
    laptop = profiles(world, clock)
    asyncio.run(laptop.profile(SHOP))
    world.pushed = "2026-10-24T08:30:00Z"
    world.files["package.json"] = json.dumps({"dependencies": {"resend": "1"}})
    again = asyncio.run(laptop.profile(SHOP))
    assert again is not None
    assert [p.id for p in again.profile.providers] == [
        "cloudflare",
        "supabase",
        "resend",
    ]  # supabase: still called in src/api/db.ts


def test_a_new_default_branch_is_read_again_too() -> None:
    world, clock = World(), Clock()
    laptop = profiles(world, clock)
    asyncio.run(laptop.profile(SHOP))
    world.branch = "trunk"
    again = asyncio.run(laptop.profile(SHOP))
    assert again is not None
    assert again.profile.branch == "trunk"


def test_a_workspace_with_no_repository_reads_nothing() -> None:
    world = World()
    assert asyncio.run(profiles(world, Clock(), repos={}).profile(SHOP)) is None
    assert world.seen == []


def test_two_opens_at_once_read_github_once() -> None:
    world = World()
    laptop = profiles(world, Clock())

    async def both() -> None:
        await asyncio.gather(laptop.profile(SHOP), laptop.profile(SHOP))

    asyncio.run(both())
    assert world.seen.count(f"/repos/{REPO}/languages") == 1


def test_reads_and_failures_are_logged_in_plain_english(capsys: pytest.CaptureFixture[str]) -> None:
    world = World()
    laptop = profiles(world, Clock())
    asyncio.run(laptop.profile(SHOP))
    world.pushed = "2026-10-24T08:30:00Z"
    world.truncated = True
    with pytest.raises(SourceError):
        asyncio.run(laptop.profile(SHOP))
    lines = [json.loads(raw) for raw in capsys.readouterr().err.splitlines()]
    events = {line["event"]: line for line in lines if line["event"].startswith("profile_")}
    assert events["profile_read"]["what"] == "Read what the shop workspace is built with: 3 providers found."
    assert events["profile_failed"]["code"] == "source.bad_response"
    assert events["profile_failed"]["what"].startswith(
        "Could not read what the shop workspace is built with: GitHub"
    )


def test_without_a_token_it_says_it_is_not_connected() -> None:
    async def known() -> dict[str, str]:
        return {}

    assert not LaptopProfiles(None, known, Clock()).connected


def test_head_is_one_call() -> None:
    world = World()

    async def body() -> Head:
        async with _client(world) as http:
            return await GitHubProfile(http, REPO).head()

    assert asyncio.run(body()) == Head("main", datetime(2026, 10, 24, 8, 0, tzinfo=UTC))
    assert world.seen == [f"/repos/{REPO}"]
