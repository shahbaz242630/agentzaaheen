"""The newest failed build's log for one repository, read only when triage asks (ADR-029).

Three read-only calls through the core's client: the repository (for its
main branch), the newest failed run there, and that run's jobs; then one
download of the first failed job's log, which GitHub serves through a
redirect to a pre-signed link on its results storage. The core follows that
one redirect without our token, and only to :data:`LOG_HOST`. The SDK cleans
and redacts the text; this module never looks inside it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from pydantic import ValidationError

from agentzaaheen.core.sdk import (
    REPO_PATTERN,
    BytesGetter,
    FailedStepLog,
    JsonGetter,
    SourceError,
    SourceErrorCode,
    make_failed_step_log,
)
from agentzaaheen.plugins.connectors.github import mapping
from agentzaaheen.plugins.connectors.github.mapping import Json

# Where job-log links point (checked 20 Oct 2026: productionresultssa5, 12, 13 and 19 seen).
LOG_HOST = re.compile(r"productionresultssa\d{1,2}\.blob\.core\.windows\.net")
JOBS_PER_PAGE = "100"


class LogGetter(JsonGetter, BytesGetter, Protocol):
    """The core's client: JSON GETs plus the one signed download."""


class GitHubBuildLog:
    """The newest failed run's first failed job log. Implements the SDK's ``BuildLogSource``."""

    def __init__(self, http: LogGetter, repo: str) -> None:
        if not re.fullmatch(REPO_PATTERN, repo) or repo.partition("/")[2] in {".", ".."}:
            raise ValueError("the repository must be written owner/name")
        self._http = http
        self._repo = repo

    @property
    def repo(self) -> str:
        """The repository this reads."""
        return self._repo

    async def fetch(self, *, now: datetime) -> FailedStepLog | None:
        """The log excerpt, or ``None`` when the main branch has no failed run with a failed job."""
        try:
            branch = mapping.branch(await self._get(f"/repos/{self._repo}", {}), "default_branch")
            page = await self._get(
                f"/repos/{self._repo}/actions/runs", {"branch": branch, "status": "failure", "per_page": "1"}
            )
            runs = mapping.objects(page.get("workflow_runs"), "workflow_runs")
            if not runs:
                return None
            run = runs[0]
            run_id = mapping.count(run, "id")
            if run_id < 1:
                raise SourceError(SourceErrorCode.BAD_RESPONSE, "github sent an unusable run id")
            jobs = await self._get(
                f"/repos/{self._repo}/actions/runs/{run_id}/jobs",
                {"filter": "latest", "per_page": JOBS_PER_PAGE},
            )
            job = next((j for j in mapping.objects(jobs.get("jobs"), "jobs") if _failed(j)), None)
            if job is None:
                return None
            raw = await self._download(f"/repos/{self._repo}/actions/jobs/{mapping.count(job, 'id')}/logs")
            return make_failed_step_log(
                source=mapping.SOURCE,
                repo=self._repo,
                run_id=run_id,
                commit=mapping.commit(run, "head_sha"),
                job=mapping.text(job, "name"),
                step=_failed_step(job),
                raw=raw,
                finished_at=mapping.time(job, "completed_at") if job.get("completed_at") else None,
                fetched_at=now,
            )
        except ValidationError:
            raise SourceError(SourceErrorCode.BAD_RESPONSE, "github log data broke a rule").at(
                "read github build log"
            ) from None
        except SourceError as exc:
            exc.at("read github build log")
            raise

    async def _get(self, path: str, params: Mapping[str, str]) -> Json:
        try:
            return await self._http.get_json(path, params)
        except SourceError as exc:
            exc.at(f"GET {path}")
            raise

    async def _download(self, path: str) -> bytes:
        try:
            return await self._http.get_redirected_bytes(path, {})
        except SourceError as exc:
            exc.at(f"GET {path} (download)")
            raise


def _failed(job: Json) -> bool:
    return job.get("conclusion") == "failure"


def _failed_step(job: Json) -> str | None:
    """The first failed step's name, if GitHub lists one."""
    steps = job.get("steps")
    if not isinstance(steps, list):
        return None
    for step in steps:
        if isinstance(step, Mapping) and step.get("conclusion") == "failure":
            name = step.get("name")
            return name if isinstance(name, str) and name else None
    return None
