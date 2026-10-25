"""Parts and fix guides (ADR-032, R4): which part of a project is down, and how to fix it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentzaaheen.core.api.uptime import uptime_card
from agentzaaheen.core.notify import UptimeWatch
from agentzaaheen.core.registry import load_registry
from agentzaaheen.core.sdk import SiteCheck, SiteRule, StatusAnswer, UptimeReport, check_site
from tools import fix_guides
from tools.chat_tools import tools_for
from tools.dashboard_sources import ListNotUsableError
from tools.fix_guides import (
    GENERAL,
    REASONS,
    FixGuideError,
    fix_guide_tool,
    fixes_list,
    guide_text,
    load_fixes,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
PROJECTS = {"zaaheen", "eva"}
GUIDE = """
zaaheen:
  name: Zaaheen
  parts:
    api:
      serves: Worker zaaheen-account
      depends_on: [Clerk, Paddle]
      logs: wrangler tail zaaheen-account
      deploy: wrangler deploy by hand
      rollback: Workers dashboard, roll back
      owner: chairman
      fixes:
        server_error:
          - Look for config_incomplete in the logs.
          - Check the secrets.
    site:
      serves: Static site
      logs: hosting panel
      deploy: site.yml
      rollback: re-run site.yml
      owner: claude
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "fixes.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# --- parts on the uptime shapes, alerts and card -----------------------------------------------------


def site(url: str, part: str | None, status: int | None) -> SiteCheck:
    answer = None if status is None else StatusAnswer(status=status, elapsed_ms=40)
    return check_site(SiteRule(url=url, part=part), answer, NOW)


def report(*sites: SiteCheck) -> UptimeReport:
    return UptimeReport(sites=sites, fetched_at=NOW)


@pytest.mark.parametrize("part", ["API", "sign in", "-api", "a" * 25, ""])
def test_a_part_is_a_short_lowercase_name(part: str) -> None:
    with pytest.raises(ValidationError):
        SiteRule(url="https://a.example", part=part)


def test_a_check_keeps_its_part() -> None:
    assert check_site(SiteRule(url="https://a.example", part="sign-in"), None, NOW).part == "sign-in"


def test_alerts_name_the_parts_down_and_back() -> None:
    watch = UptimeWatch({"zaaheen": "Zaaheen"}, threshold=1)
    a, b, c = "https://a.example", "https://b.example", "https://c.example"
    (down,) = watch.observe(
        {"zaaheen": report(site(a, "api", 503), site(b, "sign-in", None), site(c, "site", 200))}
    )
    assert down.message == (
        "api, sign-in down (did not answer, server error); 2 of 3 addresses down. Details on the dashboard."
    )
    (back,) = watch.observe(
        {"zaaheen": report(site(a, "api", 200), site(b, "sign-in", None), site(c, "site", 200))}
    )
    assert back.message == "api answering again; 2 of 3 up now."


def test_alerts_use_counts_when_a_part_is_missing() -> None:
    watch = UptimeWatch({"zaaheen": "Zaaheen"}, threshold=1)
    a, b = "https://a.example", "https://b.example"
    (down,) = watch.observe({"zaaheen": report(site(a, "api", 503), site(b, None, 503))})
    assert down.message == "2 of 2 addresses down (server error). Details on the dashboard."
    (back,) = watch.observe({"zaaheen": report(site(a, "api", 200), site(b, None, 200))})
    assert back.message == "2 addresses answering again; 2 of 2 up now."


def test_the_card_names_the_part() -> None:
    card = uptime_card(report(site("https://api.example/", "api", 503)), schedule=timedelta(hours=1), now=NOW)
    assert card.entries[0].text == "api (api.example) is down: server error (http 503)"


# --- the guides file -----------------------------------------------------------------------------------


def test_the_guides_load(tmp_path: Path) -> None:
    guides = load_fixes(write(tmp_path, GUIDE), PROJECTS)
    assert list(guides["zaaheen"].parts) == ["api", "site"]
    assert guides["zaaheen"].parts["api"].fixes["server_error"][0].startswith("Look for")


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("", "must map each project"),
        ("- a\n- b\n", "must map each project"),
        ("zaaheen: [", "not valid YAML"),
        ("nearby:\n  name: Nearby\n  parts: {}\n", "not a known project"),
        ("zaaheen:\n  name: Zaaheen\n  parts: {}\n", "parts is not usable"),
        (GUIDE.replace("    api:", "    API:"), "is not usable"),
        (GUIDE.replace("owner: chairman", "owner: someone"), "owner is not usable"),
        (GUIDE.replace("server_error:", "teapot:"), "is not usable"),
        (GUIDE.replace("      serves: Static site\n", ""), "serves is not usable"),
        (GUIDE.replace("deploy: site.yml", "deploy: site.yml\n      extra: 1"), "extra is not usable"),
        (GUIDE.replace("Check the secrets.", "x" * 301), "is not usable"),
        (
            GUIDE.replace(
                "          - Look for config_incomplete in the logs.\n          - Check the secrets.\n",
                "          []\n",
            ),
            "is not usable",
        ),
    ],
)
def test_a_broken_file_says_why(tmp_path: Path, text: str, said: str) -> None:
    with pytest.raises(FixGuideError, match=said):
        load_fixes(write(tmp_path, text), PROJECTS)


def test_a_missing_or_huge_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FixGuideError, match="cannot read"):
        load_fixes(tmp_path / "missing.yaml", PROJECTS)
    with pytest.raises(FixGuideError, match="larger than"):
        load_fixes(write(tmp_path, "#" * (fix_guides.MAX_FILE_BYTES + 1)), PROJECTS)


def test_too_many_steps_are_refused(tmp_path: Path) -> None:
    many = "".join(f"          - step {n}\n" for n in range(fix_guides.MAX_STEPS + 1))
    text = GUIDE.replace(
        "          - Look for config_incomplete in the logs.\n          - Check the secrets.\n", many
    )
    with pytest.raises(FixGuideError):
        load_fixes(write(tmp_path, text), PROJECTS)


# --- the guide as text -------------------------------------------------------------------------------------


def test_one_reason_uses_the_parts_own_steps(tmp_path: Path) -> None:
    guides = load_fixes(write(tmp_path, GUIDE), PROJECTS)
    text = guide_text(guides, "zaaheen", "api", "server_error")
    assert "Served by: Worker zaaheen-account" in text
    assert "Depends on: Clerk, Paddle" in text
    assert "Fixes are approved by: chairman" in text
    assert 'If the check says "server error":' in text
    assert "  1. Look for config_incomplete in the logs." in text
    assert "did not answer" not in text


def test_every_reason_falls_back_to_the_general_steps(tmp_path: Path) -> None:
    guides = load_fixes(write(tmp_path, GUIDE), PROJECTS)
    text = guide_text(guides, "zaaheen", "site")
    assert "Depends on: nothing listed" in text
    for reason in REASONS:
        assert GENERAL[reason][0] in text
    assert text.count("(general steps)") == len(REASONS)


def test_an_unknown_part_or_reason_gets_the_general_steps(tmp_path: Path) -> None:
    guides = load_fixes(write(tmp_path, GUIDE), PROJECTS)
    text = guide_text(guides, "zaaheen", "nope", "refused")
    assert text.startswith("There is no fix guide for zaaheen/nope")
    assert GENERAL["refused"][0] in text
    assert GENERAL["no_answer"][0] not in text
    assert GENERAL["no_answer"][0] in guide_text(guides, "eva", "api", "teapot")


def test_a_wrong_answer_has_general_steps_and_its_own_words(tmp_path: Path) -> None:
    guides = load_fixes(write(tmp_path, GUIDE), PROJECTS)
    text = guide_text(guides, "zaaheen", "site", "wrong_answer")
    assert GENERAL["wrong_answer"][0] in text
    assert "answered as something else" in text
    assert GENERAL["refused"][0] not in text


# --- the chat tool and the dashboard option -------------------------------------------------------------


def test_the_tool_offers_each_part_and_reads_its_guide(tmp_path: Path) -> None:
    tool = fix_guide_tool(load_fixes(write(tmp_path, GUIDE), PROJECTS))
    assert tool is not None
    assert tool.choices == ("zaaheen/api", "zaaheen/site")
    assert tool.trusted

    async def read() -> str:
        return await tool.run("zaaheen/api")

    text = asyncio.run(read())
    assert "Worker zaaheen-account" in text
    assert fix_guide_tool({}) is None


def test_the_chat_gets_the_tool_only_with_guides(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "agentzaaheen" / "plugins"
    registry = load_registry(root)
    guides = load_fixes(write(tmp_path, GUIDE), PROJECTS)
    with_guides, _ = tools_for(registry, root, None, {}, lambda: NOW, fixes=guides)
    without, _ = tools_for(registry, root, None, {}, lambda: NOW)
    assert "fix_guide" in {t.name for t in with_guides}
    assert "fix_guide" not in {t.name for t in without}


def test_the_dashboard_option(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert fixes_list(None, PROJECTS) == {}
    assert "zaaheen" in fixes_list(write(tmp_path, GUIDE), PROJECTS)
    with pytest.raises(ListNotUsableError):
        fixes_list(tmp_path / "missing.yaml", PROJECTS)
    assert "dashboard: cannot read missing.yaml." in capsys.readouterr().out


# --- the command ------------------------------------------------------------------------------------------


def test_the_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = str(write(tmp_path, GUIDE))
    assert fix_guides.main([path]) == 0
    assert "fixes: zaaheen: 2 parts (api, site)" in capsys.readouterr().out
    assert fix_guides.main([path, "zaaheen", "api", "server_error"]) == 0
    assert "config_incomplete" in capsys.readouterr().out
    assert fix_guides.main([path, "zaaheen", "api"]) == 0
    capsys.readouterr()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["a", "b", "c", "d", "e"],
        ["FILE", "zaaheen"],
        ["FILE", "zaaheen", "api", "teapot"],
        ["missing.yaml"],
    ],
)
def test_the_command_refuses_odd_requests(tmp_path: Path, args: list[str]) -> None:
    path = str(write(tmp_path, GUIDE))
    assert fix_guides.main([path if a == "FILE" else a for a in args]) == 1
