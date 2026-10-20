from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from yaqzan.core.api import SamplePortfolio, create_app
from yaqzan.core.api.app import CONTENT_SECURITY_POLICY
from yaqzan.core.api.code import failed_code_card
from yaqzan.core.api.overlay import CardMaker, OverlayPortfolio
from yaqzan.core.api.registry import RegistryPage, Use, registry_page, uses_from_cards
from yaqzan.core.api.views import Card, ProjectDetail
from yaqzan.core.metrics import Freshness
from yaqzan.core.registry import Invocation, Registry, load_registry
from yaqzan.core.registry.models import Problem, Skill

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
PLUGINS = Path(__file__).resolve().parents[2] / "src" / "yaqzan" / "plugins"
HASH = "a" * 64


def skill(skill_id: str, requires: tuple[str, ...]) -> Skill:
    return Skill(
        id=skill_id, description="x", invocation=Invocation.AUTO, requires=requires, fingerprint=HASH
    )


def test_the_page_carries_the_registry_and_where_each_connector_is_used() -> None:
    page = registry_page(
        load_registry(PLUGINS), {"paddle": {"zaaheen": Use.LIVE}, "ghost": {"eva": Use.LIVE}}
    )
    paddle = next(c for c in page.connectors if c.id == "paddle")
    assert [(u.project_id, u.status) for u in paddle.uses] == [("zaaheen", Use.LIVE)]
    assert next(c for c in page.connectors if c.id == "github").uses == ()
    assert paddle.endpoints[0] == "GET /subscriptions"
    assert [s.id for s in page.skills] == ["triage-failing-build"]
    assert page.skills[0].missing == ()


def test_a_skill_lists_the_connectors_it_needs_that_do_not_exist() -> None:
    registry = Registry(skills=(skill("needs-things", ("github", "supabase")),))
    assert registry_page(registry, {}).skills[0].missing == ("github", "supabase")


def test_problems_reach_the_page_without_their_codes() -> None:
    problem = Problem(kind="skill", path="skills/x", code="registry.bad_name", message="the name is wrong")
    page = registry_page(Registry(problems=(problem,)), {})
    assert page.problems[0].model_dump(mode="json") == {
        "kind": "skill",
        "path": "skills/x",
        "message": "the name is wrong",
    }


def test_connector_use_is_judged_from_the_card_it_feeds() -> None:
    sample = SamplePortfolio(NOW)
    failed: dict[tuple[str, str], CardMaker] = {
        ("eva", "github"): lambda: failed_code_card("net.timeout", source_label="GitHub")
    }
    portfolio = OverlayPortfolio(sample, failed)
    wiring = {
        "github": ("github", ("zaaheen", "eva", "no-such-project")),
        "paddle": ("no-such-card", ("zaaheen",)),
    }
    uses = uses_from_cards(portfolio.project, wiring)
    assert uses == {"github": {"zaaheen": Use.NEEDS_KEY, "eva": Use.FAILED}}


def test_a_real_fresh_card_means_live() -> None:
    card = Card(
        id="github",
        title="Code",
        source="GitHub",
        freshness=Freshness.FRESH,
        last_success_at=NOW,
        sample=False,
    )
    project = SamplePortfolio(NOW).project("zaaheen")
    assert project is not None
    detail = ProjectDetail(project=project.project, cards=(card,))
    assert uses_from_cards(lambda _: detail, {"github": ("github", ("zaaheen",))}) == {
        "github": {"zaaheen": Use.LIVE}
    }


def test_the_api_serves_the_page_read_only() -> None:
    page = registry_page(load_registry(PLUGINS), {})
    client = TestClient(create_app(SamplePortfolio(NOW), registry=page), base_url="http://127.0.0.1")
    response = client.get("/api/registry")
    assert response.status_code == 200
    assert RegistryPage.model_validate(response.json()) == page
    assert response.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["Cache-Control"] == "no-store"
    assert client.post("/api/registry", json={}).status_code == 405


def test_without_a_registry_the_page_is_empty() -> None:
    client = TestClient(create_app(SamplePortfolio(NOW)), base_url="http://127.0.0.1")
    assert client.get("/api/registry").json() == {
        "connectors": [],
        "skills": [],
        "plugins": [],
        "problems": [],
        "switches": False,
        "requests": [],
    }
