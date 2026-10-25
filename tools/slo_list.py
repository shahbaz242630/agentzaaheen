"""Read and check the local SLO list: each project's objectives, in the open OpenSLO format (ADR-032).

The list lives in ``projects/slo.yaml`` (git-ignored and path-guarded like the
other project lists, Rulebook §7b). It is a stream of OpenSLO v1 ``SLO``
documents; Agent Zaaheen reads a strict subset and refuses anything it would
otherwise have to ignore::

    apiVersion: openslo/v1
    kind: SLO
    metadata:
      name: sandoq-kin-api-speed
      displayName: Sandoq Kin API answers fast
    spec:
      service: sandoq-kin                 # an Agent Zaaheen project id
      indicator:
        metadata: {name: latency}         # availability, latency or errors
      timeWindow:
        - {duration: 28d, isRolling: true}
      budgetingMethod: Occurrences
      objectives:
        - {displayName: 95% under 800 ms, target: 0.95, op: lte, value: 800}

``op``/``value`` appear only on latency objectives (``lte``, milliseconds).

Usage:
    uv run python -m tools.slo_list projects/slo.yaml
"""

from __future__ import annotations

import re
import sys
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

import yaml

from yaqzan.core.api import SamplePortfolio
from yaqzan.core.metrics import Objective, SliKind
from yaqzan.core.registry.models import ITEM_ID

MAX_LIST_BYTES = 64 * 1024
API_VERSION = "openslo/v1"
_NAME = re.compile(ITEM_ID)
_DAYS = re.compile(r"^([1-9][0-9]?)d$")
_TOP = frozenset({"apiVersion", "kind", "metadata", "spec"})
_METADATA = frozenset({"name", "displayName"})
_SPEC = frozenset({"description", "service", "indicator", "timeWindow", "budgetingMethod", "objectives"})
_OBJECTIVE = frozenset({"displayName", "target", "op", "value"})


class SloListError(Exception):
    """The SLO list could not be used; the message says why in plain words."""


def load_slos(path: Path, projects: Collection[str]) -> tuple[Objective, ...]:
    """Every objective in ``path``; each must name a project in ``projects`` and a unique name."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise SloListError(f"the SLO list {path} could not be read") from None
    if len(raw) > MAX_LIST_BYTES:
        raise SloListError("the SLO list is too big")
    try:
        documents = [d for d in yaml.safe_load_all(raw.decode("utf-8")) if d is not None]
    except (UnicodeDecodeError, yaml.YAMLError):
        raise SloListError("the SLO list is not valid YAML") from None
    if not documents:
        raise SloListError("the SLO list has no objectives")
    objectives = tuple(_objective(doc, projects) for doc in documents)
    names = [o.name for o in objectives]
    duplicate = next((n for n in names if names.count(n) > 1), None)
    if duplicate is not None:
        raise SloListError(f"two objectives are both named {duplicate!r}")
    return objectives


def _objective(doc: object, projects: Collection[str]) -> Objective:
    top = _mapping(doc, _TOP, "an SLO document")
    if top.get("apiVersion") != API_VERSION or top.get("kind") != "SLO":
        raise SloListError(f"each document must be 'apiVersion: {API_VERSION}' and 'kind: SLO'")
    meta = _mapping(top.get("metadata"), _METADATA, "metadata")
    name = meta.get("name")
    if not isinstance(name, str) or len(name) > 64 or not _NAME.match(name):
        raise SloListError("each SLO needs a name of lowercase words joined by hyphens")
    spec = _mapping(top.get("spec"), _SPEC, f"{name}: spec")
    project = spec.get("service")
    if project not in projects:
        raise SloListError(f"{name}: names an unknown project: {project!r}")
    if spec.get("budgetingMethod") != "Occurrences":
        raise SloListError(f"{name}: only 'budgetingMethod: Occurrences' is supported")
    kind = _kind(spec.get("indicator"), name)
    target, threshold = _target(spec.get("objectives"), kind, name)
    try:
        return Objective(
            project=str(project),
            name=name,
            kind=kind,
            target=target,
            window=_window(spec.get("timeWindow"), name),
            threshold_ms=threshold,
        )
    except ValueError as exc:
        raise SloListError(f"{name}: {exc}") from None


def _mapping(value: object, allowed: frozenset[str], where: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise SloListError(f"{where} must be a mapping")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SloListError(f"{where} has fields Agent Zaaheen does not read: {', '.join(unknown)}")
    return value


def _kind(value: object, name: str) -> SliKind:
    indicator = _mapping(value, frozenset({"metadata"}), f"{name}: indicator")
    meta = _mapping(indicator.get("metadata"), frozenset({"name"}), f"{name}: indicator metadata")
    try:
        return SliKind(str(meta.get("name")))
    except ValueError:
        kinds = ", ".join(k.value for k in SliKind)
        raise SloListError(f"{name}: the indicator must be one of {kinds}") from None


def _window(value: object, name: str) -> timedelta:
    if not isinstance(value, list) or len(value) != 1:
        raise SloListError(f"{name}: give exactly one timeWindow")
    window = _mapping(value[0], frozenset({"duration", "isRolling"}), f"{name}: timeWindow")
    match = _DAYS.match(str(window.get("duration")))
    if window.get("isRolling") is not True or match is None:
        raise SloListError(f"{name}: the window must be rolling and given in days, like '28d'")
    return timedelta(days=int(match.group(1)))


def _target(value: object, kind: SliKind, name: str) -> tuple[Fraction, int | None]:
    if not isinstance(value, list) or len(value) != 1:
        raise SloListError(f"{name}: give exactly one objective")
    objective = _mapping(value[0], _OBJECTIVE, f"{name}: objective")
    raw = objective.get("target")
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        raise SloListError(f"{name}: the target must be a number like 0.995")
    try:
        target = Fraction(Decimal(str(raw)))  # 0.995 stays exactly 995/1000
    except (InvalidOperation, ValueError):
        raise SloListError(f"{name}: the target must be a number like 0.995") from None
    if kind is not SliKind.LATENCY:
        if "op" in objective or "value" in objective:
            raise SloListError(f"{name}: only a latency objective has 'op' and 'value'")
        return target, None
    threshold = objective.get("value")
    if objective.get("op") != "lte" or isinstance(threshold, bool) or not isinstance(threshold, int):
        raise SloListError(f"{name}: a latency objective needs 'op: lte' and 'value' in whole milliseconds")
    return target, threshold


def describe(objective: Objective) -> str:
    """One objective in plain words."""
    days = objective.window.days
    share = f"{float(objective.target) * 100:g}%"
    if objective.kind is SliKind.AVAILABILITY:
        hours, rest = divmod(int(objective.allowed_downtime.total_seconds()), 3600)
        budget = f"(about {hours} h {rest // 60} min down allowed)"
        what = f"{share} of external checks find it up"
    elif objective.kind is SliKind.LATENCY:
        what, budget = f"{share} of requests answered within {objective.threshold_ms} ms", ""
    else:
        what, budget = f"{share} of requests answered without a server error", ""
    return f"{objective.project} / {objective.name}: {what}, over {days} days {budget}".rstrip()


def main(argv: Sequence[str] | None = None) -> int:
    """Print every objective in the list in plain words; 1 if the list is not usable."""
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        print("usage: python -m tools.slo_list projects/slo.yaml")
        return 1
    projects = {p.id for p in SamplePortfolio(datetime.now(UTC)).projects()}
    try:
        objectives = load_slos(Path(args[0]), projects)
    except SloListError as exc:
        print(f"slo: {exc}.")
        return 1
    print(f"slo: {len(objectives)} objectives")
    for objective in objectives:
        print(f"slo: {describe(objective)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
