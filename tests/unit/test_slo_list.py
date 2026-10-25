"""The local SLO list (ADR-032): a strict OpenSLO v1 subset, refused rather than half-read."""

from __future__ import annotations

from datetime import timedelta
from fractions import Fraction
from pathlib import Path

import pytest

from agentzaaheen.core.metrics import SliKind
from tools.slo_list import SloListError, describe, load_slos, main

PROJECTS = {"sandoq-kin", "eva"}

GOOD = """\
apiVersion: openslo/v1
kind: SLO
metadata:
  name: sandoq-kin-availability
  displayName: Sandoq Kin is up
spec:
  description: external checks
  service: sandoq-kin
  indicator:
    metadata: {name: availability}
  timeWindow:
    - {duration: 28d, isRolling: true}
  budgetingMethod: Occurrences
  objectives:
    - {displayName: up, target: 0.995}
---
apiVersion: openslo/v1
kind: SLO
metadata:
  name: eva-api-speed
spec:
  service: eva
  indicator:
    metadata: {name: latency}
  timeWindow:
    - {duration: 7d, isRolling: true}
  budgetingMethod: Occurrences
  objectives:
    - {target: 0.95, op: lte, value: 800}
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "slo.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _one(**spec_changes: str) -> str:
    """One valid availability document with parts of it replaced (YAML snippets)."""
    parts = {
        "head": "apiVersion: openslo/v1\nkind: SLO",
        "name": "sandoq-kin-availability",
        "service": "sandoq-kin",
        "indicator": "{metadata: {name: availability}}",
        "window": "[{duration: 28d, isRolling: true}]",
        "method": "Occurrences",
        "objectives": "[{target: 0.995}]",
        "extra": "",
    }
    parts.update(spec_changes)
    return (
        f"{parts['head']}\nmetadata:\n  name: {parts['name']}\nspec:\n"
        f"  service: {parts['service']}\n  indicator: {parts['indicator']}\n"
        f"  timeWindow: {parts['window']}\n  budgetingMethod: {parts['method']}\n"
        f"  objectives: {parts['objectives']}\n{parts['extra']}"
    )


def test_reads_a_valid_list_exactly(tmp_path: Path) -> None:
    up, speed = load_slos(_write(tmp_path, GOOD), PROJECTS)
    assert (up.project, up.name, up.kind) == ("sandoq-kin", "sandoq-kin-availability", SliKind.AVAILABILITY)
    assert up.target == Fraction(995, 1000)  # exact, not 0.99499999...
    assert up.window == timedelta(days=28)
    assert up.threshold_ms is None
    assert (speed.kind, speed.threshold_ms, speed.window) == (SliKind.LATENCY, 800, timedelta(days=7))


def test_target_may_be_written_as_text(tmp_path: Path) -> None:
    (objective,) = load_slos(_write(tmp_path, _one(objectives='[{target: "0.99"}]')), PROJECTS)
    assert objective.target == Fraction(99, 100)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "no objectives"),
        ("---\n---\n", "no objectives"),
        ("a: [unclosed", "not valid YAML"),
        ("- just\n- a list\n", "must be a mapping"),
        (_one(head="apiVersion: openslo/v2\nkind: SLO"), "openslo/v1"),
        (_one(head="apiVersion: openslo/v1\nkind: SLI"), "kind: SLO"),
        (_one(extra="alerting: {}\n"), "does not read: alerting"),
        (_one(name="Bad_Name"), "lowercase"),
        (_one(name="x" * 65), "lowercase"),
        (_one(service="nearby"), "unknown project"),
        (_one(method="Timeslices"), "Occurrences"),
        (_one(indicator="{metadata: {name: saturation}}"), "one of availability"),
        (_one(indicator="{metadata: {name: availability}, spec: {}}"), "does not read: spec"),
        (_one(indicator="[1]"), "must be a mapping"),
        (_one(window="[]"), "exactly one timeWindow"),
        (_one(window="[{duration: 4w, isRolling: true}]"), "in days"),
        (_one(window="[{duration: 28d, isRolling: false}]"), "rolling"),
        (_one(window="[{duration: 0d, isRolling: true}]"), "in days"),
        (_one(window="[{duration: 91d, isRolling: true}]"), "between 1 and 90 days"),
        (_one(objectives="[{target: 0.9}, {target: 0.99}]"), "exactly one objective"),
        (_one(objectives="[{target: true}]"), "a number like"),
        (_one(objectives="[{target: [1]}]"), "a number like"),
        (_one(objectives='[{target: "lots"}]'), "a number like"),
        (_one(objectives="[{target: 1}]"), "between 0% and 100%"),
        (_one(objectives="[{target: 0.9, op: lte, value: 5}]"), "only a latency objective"),
        (_one(objectives="[{target: 0.9, ratio: 1}]"), "does not read: ratio"),
    ],
)
def test_refuses_what_it_cannot_read_exactly(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(SloListError, match=message):
        load_slos(_write(tmp_path, text), PROJECTS)


@pytest.mark.parametrize(
    "objectives",
    [
        "[{target: 0.95}]",
        "[{target: 0.95, op: gte, value: 800}]",
        "[{target: 0.95, op: lte, value: 800.5}]",
        "[{target: 0.95, op: lte, value: true}]",
    ],
)
def test_latency_needs_lte_and_whole_milliseconds(tmp_path: Path, objectives: str) -> None:
    text = _one(indicator="{metadata: {name: latency}}", objectives=objectives)
    with pytest.raises(SloListError, match="op: lte"):
        load_slos(_write(tmp_path, text), PROJECTS)


def test_latency_threshold_must_be_positive(tmp_path: Path) -> None:
    text = _one(indicator="{metadata: {name: latency}}", objectives="[{target: 0.95, op: lte, value: 0}]")
    with pytest.raises(SloListError, match="positive"):
        load_slos(_write(tmp_path, text), PROJECTS)


def test_duplicate_names_are_refused(tmp_path: Path) -> None:
    with pytest.raises(SloListError, match="both named 'sandoq-kin-availability'"):
        load_slos(_write(tmp_path, _one() + "---\n" + _one()), PROJECTS)


def test_missing_and_oversized_files_are_refused(tmp_path: Path) -> None:
    with pytest.raises(SloListError, match="could not be read"):
        load_slos(tmp_path / "absent.yaml", PROJECTS)
    with pytest.raises(SloListError, match="too big"):
        load_slos(_write(tmp_path, "#" * (64 * 1024 + 1)), PROJECTS)
    path = tmp_path / "bad.yaml"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(SloListError, match="not valid YAML"):
        load_slos(path, PROJECTS)


def test_describe_in_plain_words(tmp_path: Path) -> None:
    errors = _one(
        name="eva-api-errors",
        service="eva",
        indicator="{metadata: {name: errors}}",
        objectives="[{target: 0.99}]",
    )
    up, speed = load_slos(_write(tmp_path, GOOD), PROJECTS)
    (err,) = load_slos(_write(tmp_path, errors), PROJECTS)
    assert describe(up) == (
        "sandoq-kin / sandoq-kin-availability: 99.5% of external checks find it up, "
        "over 28 days (about 3 h 21 min down allowed)"
    )
    assert describe(speed) == "eva / eva-api-speed: 95% of requests answered within 800 ms, over 7 days"
    assert (
        describe(err) == "eva / eva-api-errors: 99% of requests answered without a server error, over 28 days"
    )


def test_main_prints_each_objective(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = GOOD.replace("service: eva", "service: sandoq-kin")
    assert main([str(_write(tmp_path, text))]) == 0
    out = capsys.readouterr().out
    assert "slo: 2 objectives" in out
    assert "sandoq-kin-availability" in out


def test_main_says_why_a_list_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tmp_path / "absent.yaml")]) == 1
    assert "could not be read" in capsys.readouterr().out
    assert main([]) == 1
    assert "usage" in capsys.readouterr().out
