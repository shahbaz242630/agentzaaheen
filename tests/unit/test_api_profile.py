"""The "Built with" panel's address: one workspace's languages and providers (F17 slice 5b-1)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from agentzaaheen.core.api import SamplePortfolio, create_app
from agentzaaheen.core.api.profile import Checked
from agentzaaheen.core.sdk import (
    Language,
    Provider,
    ProviderCategory,
    RepoProfile,
    SourceError,
    SourceErrorCode,
)
from agentzaaheen.core.store import StoreError, StoreErrorCode, WorkspaceRow

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 24, 9, 5, tzinfo=UTC)
PROFILE = RepoProfile(
    repo="owner/shop",
    branch="main",
    languages=(Language(name="TypeScript", bytes=2), Language(name="CSS", bytes=1)),
    providers=(
        Provider(
            id="revenuecat",
            name="RevenueCat",
            category=ProviderCategory.PAYMENTS,
            evidence=("package.json: react-native-purchases",),
        ),
    ),
    files_read=1,
    changed_at=NOW,
    read_at=NOW,
)


class FakeProfiles:
    """Profiles by workspace address; ``asked`` is every workspace the route asked about."""

    def __init__(self) -> None:
        self.connected = True
        self.repos = {"zaaheen": PROFILE}
        self.fails: Exception | None = None
        self.asked: list[str] = []

    async def profile(self, workspace: WorkspaceRow) -> Checked | None:
        self.asked.append(workspace.slug)
        if self.fails is not None:
            raise self.fails
        found = self.repos.get(workspace.slug)
        return None if found is None else Checked(profile=found, checked_at=LATER)


def get(profiles: FakeProfiles | None, slug: str = "zaaheen") -> dict[str, object]:
    app = create_app(SamplePortfolio(NOW), profiles=profiles)
    response = TestClient(app, base_url="http://127.0.0.1").get(f"/api/workspaces/{slug}/profile")
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


def test_a_workspace_with_a_repository_shows_its_languages_and_providers() -> None:
    assert get(FakeProfiles()) == {
        "state": "ready",
        "repo": "owner/shop",
        "branch": "main",
        "languages": [
            {"name": "TypeScript", "bytes": 2, "share_bp": 6667},
            {"name": "CSS", "bytes": 1, "share_bp": 3333},
        ],
        "providers": [
            {
                "id": "revenuecat",
                "name": "RevenueCat",
                "category": "payments",
                "evidence": ["package.json: react-native-purchases"],
            }
        ],
        "changed_at": "2026-10-24T09:00:00Z",
        "checked_at": "2026-10-24T09:05:00Z",
        "reason": None,
    }


def test_the_address_chooses_the_workspace_and_nothing_else() -> None:
    profiles = FakeProfiles()
    get(profiles, "eva")
    assert profiles.asked == ["eva"]
    app = create_app(SamplePortfolio(NOW), profiles=profiles)
    response = TestClient(app, base_url="http://127.0.0.1").get("/api/workspaces/nope/profile")
    assert response.status_code == 404
    assert profiles.asked == ["eva"]  # an unknown workspace is never looked up


def test_each_state_says_plainly_what_is_missing() -> None:
    assert get(FakeProfiles(), "eva")["state"] == "no_repo"
    assert get(None)["state"] == "not_connected"
    offline = FakeProfiles()
    offline.connected = False
    assert get(offline)["state"] == "not_connected"
    assert offline.asked == []


def test_a_failure_gives_its_code_never_an_error_page() -> None:
    down = FakeProfiles()
    down.fails = SourceError(SourceErrorCode.AUTH, "refused")
    assert (get(down)["state"], get(down)["reason"]) == ("failed", "source.auth")
    down.fails = StoreError(StoreErrorCode.UNAVAILABLE, "down")
    assert get(down)["reason"] == "workspaces.store_unavailable"
