"""The real GitHub source: four named read-only GETs for one repository, mapped to ``RepoHealth``.

It calls the repository itself (for its main branch), two searches (open
issues, open pull requests: their counts only, never their items) and the
main branch's recent workflow runs, through the ``JsonGetter`` the core hands
it, so it holds no token and opens no socket itself.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime

from pydantic import ValidationError

from agentzaaheen.core.sdk import REPO_PATTERN, JsonGetter, RepoHealth, SourceError, SourceErrorCode
from agentzaaheen.plugins.connectors.github import mapping
from agentzaaheen.plugins.connectors.github.mapping import Json

BASE_URL = "https://api.github.com"
ALLOWED_HOSTS = frozenset({"api.github.com"})
API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

# Enough to hold the latest run of every workflow on the main branch.
RUNS_PER_PAGE = "50"


class GitHubSource:
    """Read-only health of one GitHub repository. Implements the SDK's ``RepoSource``."""

    def __init__(self, http: JsonGetter, repo: str) -> None:
        name = repo.partition("/")[2]
        if not re.fullmatch(REPO_PATTERN, repo) or name in {".", ".."}:
            raise ValueError("the repository must be written owner/name")
        self._http = http
        self._repo = repo

    @property
    def source(self) -> str:
        """The connector id."""
        return mapping.SOURCE

    @property
    def repo(self) -> str:
        """The repository this source reads."""
        return self._repo

    async def fetch(self, *, now: datetime) -> RepoHealth:
        """Open issues, open pull requests, and how the newest commit on the main branch built."""
        try:
            branch = mapping.branch(await self._get(f"/repos/{self._repo}", {}), "default_branch")
            issues = mapping.search_total(await self._search("is:issue"))
            pulls = mapping.search_total(await self._search("is:pr"))
            page = await self._get(
                f"/repos/{self._repo}/actions/runs", {"branch": branch, "per_page": RUNS_PER_PAGE}
            )
            built = mapping.build(mapping.objects(page.get("workflow_runs"), "workflow_runs"), self._repo)
            return RepoHealth(
                source=mapping.SOURCE,
                repo=self._repo,
                default_branch=branch,
                open_issues=issues,
                open_pull_requests=pulls,
                build=built.state,
                build_commit=built.commit,
                failing_workflows=built.failing,
                build_finished_at=built.finished_at,
                fetched_at=now,
            )
        except ValidationError:
            raise SourceError(SourceErrorCode.BAD_RESPONSE, "github data broke a code-health rule").at(
                "read github data"
            ) from None
        except SourceError as exc:
            exc.at("read github data")
            raise

    async def _search(self, kind: str) -> Json:
        query = {"q": f"repo:{self._repo} {kind} is:open", "per_page": "1"}
        return await self._get("/search/issues", query)

    async def _get(self, path: str, params: Mapping[str, str]) -> Json:
        try:
            return await self._http.get_json(path, params)
        except SourceError as exc:
            exc.at(f"GET {path}")
            raise
