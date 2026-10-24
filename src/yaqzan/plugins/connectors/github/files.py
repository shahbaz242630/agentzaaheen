"""What a repository holds, for the scout (ADR-039, F17 slice 5a), and which repositories the token covers.

``GitHubFiles`` reads one repository's main branch: its file tree (every path,
secret files left out) and the text of the files the SDK's ``pick_files``
chooses, within the SDK's caps. Only these fields are read: a repository's
``default_branch``; a tree's ``truncated`` and each entry's ``path``, ``type``
and ``size``; a file's ``encoding`` and ``content``. File text is written by
anyone who can push, so it is handed on as untrusted text (the SDK's
``RepoFile``), never as a fact.

``GitHubRepos`` lists the repositories the read-only token covers (their
``full_name`` only), so the dashboard offers only repositories it can read.

``GitHubProfile`` reads what a project is built with (F17 slice 5b-1): the
languages GitHub counted (``/languages``: names and byte counts only) and the
providers the SDK's rules find in the file tree, in a few manifests
(``package.json``, workflow files...) and in a few code and config files
(providers' web addresses). That text is only matched by those rules, never
kept. ``head()`` is one cheap call (the default branch and the last push time,
``pushed_at``), so a caller reads the full profile only when the repository
changed.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from pydantic import ValidationError

from yaqzan.core.sdk import (
    MAX_CODE_BYTES,
    MAX_FILE_BYTES,
    MAX_FILE_CHARS,
    MAX_LANGUAGES,
    MAX_MANIFEST_BYTES,
    MAX_PATHS,
    MAX_TOTAL_CHARS,
    REPO_PATTERN,
    JsonReader,
    Language,
    RepoBundle,
    RepoFile,
    RepoProfile,
    SourceError,
    SourceErrorCode,
    detect,
    is_plain_path,
    is_secret_path,
    pick_code,
    pick_files,
    pick_manifests,
)
from yaqzan.plugins.connectors.github import mapping
from yaqzan.plugins.connectors.github.mapping import Json

REPOS_PER_PAGE = "100"
AT_ONCE = 6  # files read from GitHub at the same time for a profile (well inside its limits)


def _bad(what: str) -> SourceError:
    return SourceError(SourceErrorCode.BAD_RESPONSE, f"github sent an unusable {what}")


class GitHubFiles:
    """The paths and a few files of one repository's main branch, for the scout. Read-only."""

    def __init__(self, http: JsonReader, repo: str) -> None:
        self._http = http
        self._repo = _checked(repo)

    async def bundle(self, *, now: datetime) -> RepoBundle:
        """Every path (secret files left out) and the text of the files the scout reads."""
        try:
            branch = mapping.branch(await _get(self._http, f"/repos/{self._repo}", {}), "default_branch")
            sizes = await _tree(self._http, self._repo, branch)
            shown = [p for p in sizes if not is_secret_path(p)]
            listed = shown[:MAX_PATHS]
            readable = [p for p in listed if sizes[p] <= MAX_FILE_BYTES]
            files = await self._read(pick_files(readable), branch)
            return RepoBundle(
                repo=self._repo,
                branch=branch,
                paths=tuple(listed),
                total_files=len(shown),
                secret_files=len(sizes) - len(shown),
                files=tuple(files),
                read_at=now,
            )
        except ValidationError:
            raise _bad("file list").at("read github files") from None
        except SourceError as exc:
            exc.at("read github files")
            raise

    async def _read(self, paths: list[str], branch: str) -> list[RepoFile]:
        files: list[RepoFile] = []
        room = MAX_TOTAL_CHARS
        for path in paths:
            if room <= 0:
                break
            text = await _text(self._http, self._repo, path, branch)
            if text is None:
                continue  # not text (an image, a binary): skipped, not guessed
            keep = min(MAX_FILE_CHARS, room)
            files.append(RepoFile(path=path, text=text[:keep], cut=len(text) > keep))
            room -= min(len(text), keep)
        return files


@dataclass(frozen=True)
class Head:
    """A repository's default branch, and when GitHub last saw a push (``None`` if it did not say)."""

    branch: str
    changed_at: datetime | None


class GitHubProfile:
    """The languages and providers of one repository's main branch, found by code. Read-only."""

    def __init__(self, http: JsonReader, repo: str) -> None:
        self._http = http
        self._repo = _checked(repo)

    async def head(self) -> Head:
        """The default branch and the last push time: one call, to tell whether the profile changed."""
        try:
            found = await _get(self._http, f"/repos/{self._repo}", {})
            pushed = None if found.get("pushed_at") is None else mapping.time(found, "pushed_at")
            return Head(branch=mapping.branch(found, "default_branch"), changed_at=pushed)
        except SourceError as exc:
            exc.at("read github profile")
            raise

    async def profile(self, head: Head, *, now: datetime) -> RepoProfile:
        """The languages GitHub counted and the providers the SDK's rules find on ``head``'s branch."""
        try:
            sizes = await _tree(self._http, self._repo, head.branch)
            paths = [p for p in sizes if not is_secret_path(p)]
            manifests = pick_manifests([p for p in paths if sizes[p] <= MAX_MANIFEST_BYTES])
            code = [
                p for p in pick_code([p for p in paths if sizes[p] <= MAX_CODE_BYTES]) if p not in manifests
            ]
            wanted = manifests + code
            limit = asyncio.Semaphore(AT_ONCE)

            async def one(path: str) -> str | None:
                async with limit:
                    return await _text(self._http, self._repo, path, head.branch)

            found = await asyncio.gather(*(one(path) for path in wanted))
            # Matched by the SDK's rules below, then dropped.
            texts = {path: text for path, text in zip(wanted, found, strict=True) if text is not None}
            languages = _languages(await _get(self._http, f"/repos/{self._repo}/languages", {}))
            return RepoProfile(
                repo=self._repo,
                branch=head.branch,
                languages=languages,
                providers=detect(paths, texts),
                files_read=len(texts),
                changed_at=head.changed_at,
                read_at=now,
            )
        except ValidationError:
            raise _bad("profile").at("read github profile") from None
        except SourceError as exc:
            exc.at("read github profile")
            raise


class GitHubRepos:
    """The repositories the token covers, by ``owner/name``. Read-only."""

    def __init__(self, http: JsonReader) -> None:
        self._http = http

    async def names(self) -> list[str]:
        """Every repository the token can read, sorted (one page of 100: plenty for one person)."""
        try:
            found = await self._http.get_json_list(
                "/user/repos", {"per_page": REPOS_PER_PAGE, "sort": "full_name"}
            )
        except SourceError as exc:
            exc.at("list github repositories")
            raise
        names = {mapping.text(mapping.obj(item, "repository"), "full_name") for item in found}
        if any(not re.fullmatch(REPO_PATTERN, n) for n in names):
            raise _bad("repository name").at("list github repositories")
        return sorted(names, key=str.lower)


def _checked(repo: str) -> str:
    name = repo.partition("/")[2]
    if not re.fullmatch(REPO_PATTERN, repo) or name in {".", ".."}:
        raise ValueError("the repository must be written owner/name")
    return repo


async def _get(http: JsonReader, path: str, params: Mapping[str, str]) -> Json:
    try:
        return await http.get_json(path, params)
    except SourceError as exc:
        exc.at(f"GET {path}")
        raise


async def _tree(http: JsonReader, repo: str, branch: str) -> dict[str, int]:
    """Every file's path and size on ``branch``, in the tree's order; refused if GitHub cut it short."""
    tree = await _get(http, f"/repos/{repo}/git/trees/{quote(branch, safe='')}", {"recursive": "1"})
    if tree.get("truncated") is not False:
        raise _bad("file tree (it was cut short)")
    return _blobs(mapping.objects(tree.get("tree"), "file tree"))


async def _text(http: JsonReader, repo: str, path: str, branch: str) -> str | None:
    """One file's text on ``branch``, or ``None`` if it is not UTF-8 text."""
    return _decoded(await _get(http, f"/repos/{repo}/contents/{quote(path)}", {"ref": branch}))


def _languages(found: Json) -> tuple[Language, ...]:
    """GitHub's byte count per language, largest first (ties by name); a bad count is refused."""
    languages: list[Language] = []
    for name, size in found.items():
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise _bad("language count")
        languages.append(Language(name=name, bytes=size))
    languages.sort(key=lambda lang: (-lang.bytes, lang.name))
    return tuple(languages[:MAX_LANGUAGES])


def _blobs(entries: list[Json]) -> dict[str, int]:
    """Each file's path and size, in the tree's order. Folders and links are not files."""
    found: dict[str, int] = {}
    for entry in entries:
        if entry.get("type") != "blob":
            continue
        path, size = entry.get("path"), entry.get("size")
        if not isinstance(path, str) or not is_plain_path(path):
            continue  # a name we cannot pass on safely is left out, never repaired
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise _bad("file size")
        found[path] = size
    return found


def _decoded(found: Json) -> str | None:
    """A file's text, or ``None`` if it is not UTF-8 text."""
    content = found.get("content")
    if found.get("encoding") != "base64" or not isinstance(content, str):
        raise _bad("file")
    try:
        raw = base64.b64decode(content, validate=False)
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return None if "\x00" in text else text
