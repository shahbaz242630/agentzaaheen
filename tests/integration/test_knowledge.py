"""The knowledge bank's product documents on a real database (ADR-037, F12 slice 1).

Passages sit behind each workspace's walls; a changed file is replaced whole,
an unlisted one is dropped; word search finds the right passage and a vague
question still gets each document's opening. The update job reads a pretend
GitHub: only listed documents, only when changed.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.store import Database, DatabaseSettings, KnowledgeRows, StoreError, StoreErrorCode
from tests.integration.conftest import connect_as, run
from tests.integration.test_dashboard_switches import _switches, laptop_url
from tools import knowledge_check, laptop_db
from tools.dashboard_knowledge import KnowledgeSync, LaptopKnowledge

__all__ = ["laptop_url"]  # the pytest fixture shared with the switches tests

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
BY_SLUG = {w.slug: w for w in laptop_db.LAPTOP_WORKSPACES}
ZAAHEEN, SANDOQ = BY_SLUG["zaaheen"], BY_SLUG["sandoq-kin"]
SHA_A, SHA_B = "a" * 40, "b" * 40


def rows(db: Database, slug: str) -> KnowledgeRows:
    return KnowledgeRows(db.scoped(laptop_db.LAPTOP_TENANT, BY_SLUG[slug].workspace_id))


async def opened(url: SecretStr) -> Database:
    return await Database.open(DatabaseSettings(dsn=url))


def test_passages_stay_behind_their_workspace_and_a_change_replaces_them_whole(laptop_url: SecretStr) -> None:
    async def body() -> list[object]:
        db = await opened(laptop_url)
        try:
            z, s = rows(db, "zaaheen"), rows(db, "sandoq-kin")
            await z.replace(
                "README.md",
                SHA_A,
                [("Zaaheen", "A private memory for AI apps."), ("Zaaheen > Price", "Old.")],
                at=NOW,
            )
            await s.replace("README.md", SHA_A, [("Sandoq Kin", "A family vault for documents.")], at=NOW)
            seen_by_sandoq = [p.body for p in await s.search("private memory")]
            await z.replace("README.md", SHA_B, [("Zaaheen", "A private memory, now on Mac too.")], at=NOW)
            after = [(p.title, p.body) for p in await z.search("memory mac")]
            known = await z.known()
            return [seen_by_sandoq, after, known]
        finally:
            await db.close()

    seen_by_sandoq, after, known = run(body())
    assert seen_by_sandoq == ["A family vault for documents."]  # only its own opening, never Zaaheen's
    assert after == [("Zaaheen", "A private memory, now on Mac too.")]  # the old passages are gone
    assert known == {"README.md": SHA_B}


def test_word_search_ranks_the_best_passage_first_and_tops_up_with_openings(laptop_url: SecretStr) -> None:
    async def body() -> list[list[str]]:
        db = await opened(laptop_url)
        try:
            z = rows(db, "zaaheen")
            await z.replace(
                "README.md",
                SHA_A,
                [
                    ("Zaaheen", "Zaaheen gives your AI apps one private memory."),
                    ("Zaaheen > Pricing", "Seven days free, then a monthly or a yearly plan."),
                    ("Zaaheen > Security", "Memories are encrypted on your own computer."),
                ],
                at=NOW,
            )
            await z.replace(
                "site/pricing.astro", SHA_A, [("Pricing", "Pay monthly or yearly; cancel any time.")], at=NOW
            )
            priced = [p.title for p in await z.search("How much is the yearly plan?")]
            vague = [p.title for p in await z.search("what is this for?")]
            nothing = [p.title for p in await z.search("")]
            return [priced, vague, nothing]
        finally:
            await db.close()

    priced, vague, nothing = run(body())
    assert priced[:2] == ["Zaaheen > Pricing", "Pricing"]  # "yearly" and "plan" (stemmed) match
    assert len(priced) == 3
    assert vague == ["Zaaheen", "Pricing"]  # nothing matches: each document's opening
    assert nothing == ["Zaaheen", "Pricing"]


def test_dropping_documents_removes_only_those(laptop_url: SecretStr) -> None:
    async def body() -> list[object]:
        db = await opened(laptop_url)
        try:
            z = rows(db, "zaaheen")
            await z.replace("a.md", SHA_A, [("A", "Alpha.")], at=NOW)
            await z.replace("b.md", SHA_A, [("B", "Beta.")], at=NOW)
            gone = await z.drop(["a.md", "never-kept.md"])
            return [gone, await z.known()]
        finally:
            await db.close()

    assert run(body()) == [1, {"b.md": SHA_A}]


def test_the_database_refuses_odd_rows(laptop_url: SecretStr) -> None:
    async def body() -> list[str]:
        db = await opened(laptop_url)
        refused: list[str] = []
        try:
            z = rows(db, "zaaheen")
            for source, sha, piece in [
                ("../outside.md", SHA_A, ("T", "B")),
                ("a.md", "not-a-sha", ("T", "B")),
                ("a.md", SHA_A, ("T", "x" * 4001)),
                ("a.md", SHA_A, ("", "B")),
            ]:
                try:
                    await z.replace(source, sha, [piece], at=NOW)
                except StoreError as exc:
                    refused.append(exc.code.value)
            return refused
        finally:
            await db.close()

    assert len(run(body())) == 4


def test_the_wall_hides_another_workspaces_passages_even_from_hand_written_sql(laptop_url: SecretStr) -> None:
    async def body() -> int:
        db = await opened(laptop_url)
        try:
            await rows(db, "sandoq-kin").replace(
                "README.md", SHA_A, [("Sandoq Kin", "A family vault.")], at=NOW
            )
        finally:
            await db.close()
        conn = await connect_as(laptop_url.get_secret_value(), laptop_db.LAPTOP_TENANT, ZAAHEEN.workspace_id)
        try:
            seen: int = await conn.fetchval("SELECT count(*) FROM agentzaaheen.knowledge_pieces")
            return seen
        finally:
            await conn.close()

    assert run(body()) == 0


def test_a_passage_cannot_be_written_into_another_workspace(laptop_url: SecretStr) -> None:
    async def body() -> None:
        conn = await connect_as(laptop_url.get_secret_value(), laptop_db.LAPTOP_TENANT, ZAAHEEN.workspace_id)
        try:
            await conn.execute(
                "INSERT INTO agentzaaheen.knowledge_pieces"
                " (tenant_id, project_id, section, source, blob_sha, position, title, body, read_at)"
                " VALUES ($1, $2, 'products', 'a.md', $3, 0, 'T', 'B', $4)",
                laptop_db.LAPTOP_TENANT,
                SANDOQ.workspace_id,
                SHA_A,
                NOW,
            )
        finally:
            await conn.close()

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(body())


def test_the_best_match_comes_first_wherever_it_sits_and_three_are_shown(laptop_url: SecretStr) -> None:
    async def body() -> list[list[str]]:
        db = await opened(laptop_url)
        try:
            z = rows(db, "zaaheen")
            weak = [(f"A > Part {n}", f"Part {n} mentions the plan once.") for n in range(5)]
            await z.replace("a.md", SHA_A, weak, at=NOW)
            await z.replace(
                "z.md",
                SHA_A,
                [("Intro", "Hello."), ("Plans", "The yearly plan: yearly plan prices.")],
                at=NOW,
            )
            plain = [p.title for p in await z.search("yearly plan")]
            led = [p.title for p in await z.search("yearly plan", overview="z.md")]
            return [plain, led]
        finally:
            await db.close()

    plain, led = run(body())
    assert plain[0] == "Plans"  # last by name and position, first by how well it matches
    assert len(plain) == 3
    assert led[0] == "Plans"  # the overview's first real passage (Hello. is too short to count)
    assert len(led) == 3
    assert led.count("Plans") == 1  # never shown twice


# ---- the update job, reading a pretend GitHub


def _file(text: str) -> dict[str, Any]:
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


class PretendGitHub:
    """One repository per project: a tree with blob ids and the files' text; counts each file read."""

    def __init__(self) -> None:
        self.files = {
            "shahbaz242630/zaaheen": {
                "README.md": (SHA_A, "# Zaaheen\n\nA private memory for your AI apps."),
                "site/pages/products.astro": (SHA_A, "<h1>Products</h1><p>The memory app.</p>"),
                "site/pages/logo.png": (SHA_A, "not a document"),
                ".env": (SHA_A, "SECRET=1"),
            },
            "shahbaz242630/Document-Vault": {"README.md": (SHA_A, "# Sandoq Kin\n\nA family vault.")},
        }
        self.reads: list[str] = []
        self.broken: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for repo, files in self.files.items():
            base = f"/repos/{repo}"
            if repo in self.broken and path.startswith(base):
                return httpx.Response(500)
            if path == base:
                return httpx.Response(200, json={"default_branch": "main"})
            if path == f"{base}/git/trees/main":
                tree = [{"type": "blob", "path": p, "size": len(t), "sha": s} for p, (s, t) in files.items()]
                return httpx.Response(200, json={"truncated": False, "tree": tree})
            if path.startswith(f"{base}/contents/"):
                name = path.removeprefix(f"{base}/contents/")
                self.reads.append(f"{repo}:{name}")
                return httpx.Response(200, json=_file(files[name][1]))
        return httpx.Response(404)


def _sync(url: SecretStr, github: PretendGitHub, listed: dict[str, tuple[str, ...]]) -> list[list[str]]:
    async def body() -> list[list[str]]:
        switches = _switches(url)
        try:
            job = KnowledgeSync(
                listed,
                lambda: laptop_db.LAPTOP_WORKSPACES,
                switches.repos_in_use,
                switches.knowledge,
                token=SecretStr("t" * 40),
                clock=lambda: NOW,
                transport=httpx.MockTransport(github),
            )
            await job.sync()
            first = list(github.reads)
            await job.sync()  # nothing changed: nothing read again
            second = github.reads[len(first) :]
            search = LaptopKnowledge(switches.knowledge)
            found = [p.title for p in await search.search(ZAAHEEN, "memory app")]
            return [first, second, found]
        finally:
            await switches.close()

    return run(body())


def test_the_job_reads_only_listed_documents_and_only_when_they_changed(laptop_url: SecretStr) -> None:
    github = PretendGitHub()
    listed = {"zaaheen": ("README.md", "site/pages/", ".env", "gone.md"), "sandoq-kin": ("README.md",)}
    first, second, found = _sync(laptop_url, github, listed)
    assert sorted(first) == [
        "shahbaz242630/Document-Vault:README.md",
        "shahbaz242630/zaaheen:README.md",
        "shahbaz242630/zaaheen:site/pages/products.astro",
    ]  # never the picture, never .env, nothing for a missing file
    assert second == []
    assert sorted(found) == ["Products", "Zaaheen"]


def test_a_changed_file_is_read_again_and_an_unlisted_one_dropped(laptop_url: SecretStr) -> None:
    github = PretendGitHub()
    _sync(laptop_url, github, {"zaaheen": ("README.md", "site/pages/")})
    github.files["shahbaz242630/zaaheen"]["README.md"] = (SHA_B, "# Zaaheen\n\nNow on Mac too.")
    github.reads.clear()
    first, _, found = _sync(laptop_url, github, {"zaaheen": ("README.md",)})
    assert first == ["shahbaz242630/zaaheen:README.md"]
    assert found == ["Zaaheen"]  # the products page left the list, so its passages went


def test_one_project_failing_never_stops_the_others(
    laptop_url: SecretStr, capsys: pytest.CaptureFixture[str]
) -> None:
    github = PretendGitHub()
    github.broken.add("shahbaz242630/zaaheen")
    first, _, found = _sync(laptop_url, github, {"zaaheen": ("README.md",), "sandoq-kin": ("README.md",)})
    assert first == ["shahbaz242630/Document-Vault:README.md"]
    assert found == []
    events = [json.loads(raw) for raw in capsys.readouterr().err.splitlines() if raw.startswith("{")]
    failed = [(e["event"], e["workspace"]) for e in events if e.get("event", "").startswith("knowledge_sync")]
    assert ("knowledge_sync_failed", "zaaheen") in failed  # said as a GitHub failure, not a crash
    assert all(event != "knowledge_sync_crashed" for event, _ in failed)


def test_a_crash_in_one_project_is_said_and_the_others_still_read(
    laptop_url: SecretStr, capsys: pytest.CaptureFixture[str]
) -> None:
    async def body() -> list[str]:
        switches = _switches(laptop_url)
        github = PretendGitHub()
        try:

            async def odd_rows(workspace: Any) -> KnowledgeRows:
                if workspace.slug == "zaaheen":
                    raise RuntimeError("our own bug")
                return await switches.knowledge(workspace)

            job = KnowledgeSync(
                {"zaaheen": ("README.md",), "sandoq-kin": ("README.md",)},
                lambda: laptop_db.LAPTOP_WORKSPACES,
                switches.repos_in_use,
                odd_rows,
                token=SecretStr("t" * 40),
                clock=lambda: NOW,
                transport=httpx.MockTransport(github),
            )
            await job.start()  # the first round runs at once
            for _ in range(100):
                if github.reads:
                    break
                await asyncio.sleep(0.05)
            await job.close()
            return github.reads
        finally:
            await switches.close()

    assert run(body()) == ["shahbaz242630/Document-Vault:README.md"]
    events = [json.loads(raw) for raw in capsys.readouterr().err.splitlines() if raw.startswith("{")]
    assert any(e.get("event") == "knowledge_sync_crashed" and e["kind"] == "RuntimeError" for e in events)


def test_a_search_that_cannot_reach_the_database_gives_no_passages(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def broken(_workspace: Any) -> KnowledgeRows:
        raise StoreError(StoreErrorCode.UNAVAILABLE, "down")

    assert run(LaptopKnowledge(broken).search(ZAAHEEN, "anything")) == []
    assert '"knowledge_search_failed"' in capsys.readouterr().err


def test_the_repository_list_failing_reads_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    async def no_repos() -> dict[str, str]:
        raise StoreError(StoreErrorCode.UNAVAILABLE, "down")

    async def no_rows(_workspace: Any) -> KnowledgeRows:
        raise AssertionError("never asked")

    job = KnowledgeSync(
        {"zaaheen": ("README.md",)},
        lambda: laptop_db.LAPTOP_WORKSPACES,
        no_repos,
        no_rows,
        token=SecretStr("t" * 40),
        clock=lambda: NOW,
    )
    run(job.sync())
    assert '"workspace": "all"' in capsys.readouterr().err


# ---- the scored test set (tools.knowledge_check)


def _questions(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "questions.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_test_set_scores_real_searches_and_fails_below_the_pass_mark(
    laptop_url: SecretStr, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def body() -> None:
        db = await opened(laptop_url)
        try:
            await rows(db, "zaaheen").replace(
                "README.md",
                SHA_A,
                [("Zaaheen", "One private memory for all your AI apps, on your computer.")],
                at=NOW,
            )
        finally:
            await db.close()

    run(body())

    async def prepare() -> SecretStr:
        return laptop_url

    monkeypatch.setattr(laptop_db, "prepare", prepare)
    listed = tmp_path / "knowledge.yaml"
    listed.write_text("zaaheen: [README.md]\n", encoding="utf-8")
    questions = _questions(
        tmp_path,
        "- {project: zaaheen, q: 'what is it for?', expect: private memory}\n"
        "- {project: zaaheen, q: 'what does it cost?', expect: AED|dollars}\n"
        "- {project: zaaheen, q: 'prices?', expect: AED, known_gap: true}\n"
        "- {project: nowhere, q: 'anything?', expect: x}\n",
    )
    argv = ["--questions", str(questions), "--knowledge", str(listed)]
    assert knowledge_check.main([*argv, "--min", "1"]) == 0
    out = capsys.readouterr().out
    assert "PASS [zaaheen] what is it for?" in out
    assert "MISS [zaaheen] what does it cost?" in out
    assert "GAP  [zaaheen] prices?" in out
    assert "score 1/3 (pass mark 1)" in out
    assert knowledge_check.main([*argv, "--min", "2"]) == 1


@pytest.mark.parametrize(
    "text",
    ["[]", "- {project: zaaheen, q: x}", "just words", "- 3"],
)
def test_a_broken_test_set_is_refused(tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert knowledge_check.main(["--questions", str(_questions(tmp_path, text)), "--min", "1"]) == 1
    assert capsys.readouterr().out.startswith("knowledge_check: ")


def test_the_test_set_says_when_the_laptop_database_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def not_running() -> None:
        return None

    monkeypatch.setattr(laptop_db, "prepare", not_running)
    listed = tmp_path / "knowledge.yaml"
    listed.write_text("zaaheen: [README.md]\n", encoding="utf-8")
    questions = _questions(tmp_path, "- {project: zaaheen, q: 'x?', expect: y}\n")
    assert (
        knowledge_check.main(["--questions", str(questions), "--knowledge", str(listed), "--min", "1"]) == 1
    )
    assert "laptop database is not running" in capsys.readouterr().out


def test_a_word_in_most_passages_does_not_outweigh_the_telling_one(laptop_url: SecretStr) -> None:
    async def body() -> list[str]:
        db = await opened(laptop_url)
        try:
            s = rows(db, "sandoq-kin")
            busy = [
                (f"Flows > Step {n}", "The user taps. The user confirms. The user waits.") for n in range(9)
            ]
            telling = [("What we are building", "What a family needs if the user dies suddenly.")]
            await s.replace("brief.md", SHA_A, [*busy, *telling], at=NOW)
            return [p.title for p in await s.search("what happens if the user dies?")]
        finally:
            await db.close()

    assert run(body())[0] == "What we are building"


def test_in_a_small_project_or_when_every_word_is_common_all_words_still_count(laptop_url: SecretStr) -> None:
    async def body() -> list[list[str]]:
        db = await opened(laptop_url)
        try:
            z, s = rows(db, "zaaheen"), rows(db, "sandoq-kin")
            await z.replace("a.md", SHA_A, [("A", "The user taps."), ("B", "The user waits.")], at=NOW)
            small = [p.title for p in await z.search("user")]
            common = [(f"Step {n}", "The user taps.") for n in range(10)]
            await s.replace("b.md", SHA_A, common, at=NOW)
            every = [p.title for p in await s.search("user taps")]
            return [small, every]
        finally:
            await db.close()

    small, every = run(body())
    assert sorted(small) == ["A", "B"]  # 2 passages: "user" is in all of them and still counts
    assert len(every) == 3  # every word is common: none is left out, so all still count
    assert all(t.startswith("Step") for t in every)
