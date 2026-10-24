"""What a workspace is built with: its languages and outside providers (F17 slice 5b-1).

``GET /api/workspaces/{slug}/profile`` answers through the workspace door, so
it only ever reads the repository saved for that one workspace. The answer
says plainly which state it is in:

- ``ready``: the languages (with shares that add up to exactly 100%) and the
  providers code found, each with its evidence;
- ``no_repo``: the workspace has no repository connected;
- ``not_connected``: no GitHub token was given at start;
- ``failed``: GitHub (or the laptop database, for the workspace's
  repository) could not be read just now; ``reason`` is the code.

Each request checks whether the repository changed (one cheap call), so the
page is up to date as soon as it opens; the full read runs only after a push.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from fastapi import FastAPI
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from yaqzan.core.api.workspaces import InWorkspace
from yaqzan.core.sdk import REPO_PATTERN, ProviderCategory, RepoProfile, SourceError
from yaqzan.core.store import StoreError, WorkspaceRow

ProfileState = Literal["ready", "no_repo", "not_connected", "failed"]


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LanguageOut(_View):
    """One language: its byte count, and its share in basis points (10,000 = 100%)."""

    name: str = Field(max_length=60)
    bytes: int = Field(ge=0)
    share_bp: int = Field(ge=0, le=10_000)


class ProviderOut(_View):
    """One outside provider code found, and why (a path, or ``path: package``)."""

    id: str = Field(max_length=40)
    name: str = Field(max_length=60)
    category: ProviderCategory
    evidence: tuple[str, ...]


class ProfileOut(_View):
    """What the workspace page's "Built with" panel shows."""

    state: ProfileState
    repo: str | None = Field(default=None, pattern=REPO_PATTERN)
    branch: str | None = None
    languages: tuple[LanguageOut, ...] = ()
    providers: tuple[ProviderOut, ...] = ()
    changed_at: AwareDatetime | None = None  # the repository's last push
    checked_at: AwareDatetime | None = None  # when this answer checked for changes
    reason: str | None = None  # the error code, when ``failed``


@dataclass(frozen=True)
class Checked:
    """A profile, and when it was last checked against the repository (may be newer than its read)."""

    profile: RepoProfile
    checked_at: datetime


class WorkspaceProfiles(Protocol):
    """What the profile route needs (``LaptopProfiles`` in real use)."""

    @property
    def connected(self) -> bool:
        """True if a GitHub token was given, so repositories can be read."""
        ...

    async def profile(self, workspace: WorkspaceRow) -> Checked | None:
        """The workspace's profile, or ``None`` if it has no repository. Raises ``SourceError``."""
        ...


def profile_out(checked: Checked) -> ProfileOut:
    """The ``ready`` answer for one profile."""
    profile = checked.profile
    return ProfileOut(
        state="ready",
        repo=profile.repo,
        branch=profile.branch,
        languages=tuple(
            LanguageOut(name=lang.name, bytes=lang.bytes, share_bp=share)
            for lang, share in zip(profile.languages, profile.shares(), strict=True)
        ),
        providers=tuple(
            ProviderOut(id=p.id, name=p.name, category=p.category, evidence=p.evidence)
            for p in profile.providers
        ),
        changed_at=profile.changed_at,
        checked_at=checked.checked_at,
    )


def profile_routes(app: FastAPI, profiles: WorkspaceProfiles | None) -> None:
    """The route for the workspace page's "Built with" panel."""

    @app.get("/api/workspaces/{slug}/profile")
    async def get_profile(workspace: InWorkspace) -> ProfileOut:
        if profiles is None or not profiles.connected:
            return ProfileOut(state="not_connected")
        try:
            checked = await profiles.profile(workspace)
        except SourceError as exc:
            return ProfileOut(state="failed", reason=exc.code.value)
        except StoreError:
            return ProfileOut(state="failed", reason="workspaces.store_unavailable")
        return ProfileOut(state="no_repo") if checked is None else profile_out(checked)
