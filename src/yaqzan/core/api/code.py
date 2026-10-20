"""Turn a repository's code health into its dashboard card (F03 slice 3).

The card shows counts and one line about the main branch's newest build. Its
freshness is judged when it is shown: once the read is older than its rule
allows, every number turns "unknown" and the build line is dropped, so an old
"build passed" can never look current. A read that failed shows why instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from yaqzan.core import metrics
from yaqzan.core.api.views import Card, Count, Entry, Metric, NotKnown, Tone
from yaqzan.core.sdk import BuildState, RepoHealth, explain

CARD_ID = "github"
TITLE = "Code"
LABELS = ("Open issues", "Open pull requests", "Failing workflows")
_MAX_ENTRY = 160
# A branch name longer than this is not repeated on the card (it would reach the chat model as text).
_MAX_BRANCH = 40


def code_card(health: RepoHealth, *, source_label: str, schedule: timedelta, now: datetime) -> Card:
    """One repository's card, fresh or (once too old) all unknown."""
    fresh = metrics.freshness(health.fetched_at, schedule, now)
    seen = health.fetched_at
    if not fresh.usable:
        gone = NotKnown(reason=f"freshness.{fresh.value}")
        unknown = tuple(Metric(label=label, value=gone) for label in LABELS)
        return _card(fresh, unknown, (), source_label=source_label, last_success_at=seen)
    branch = f"{_branch(health)}, latest run of each workflow"
    numbers = (
        Metric(label=LABELS[0], value=Count(value=health.open_issues)),
        Metric(label=LABELS[1], value=Count(value=health.open_pull_requests)),
        Metric(label=LABELS[2], value=Count(value=health.failing_workflows), detail=branch),
    )
    return _card(fresh, numbers, (_build_line(health),), source_label=source_label, last_success_at=seen)


def failed_code_card(code: str, *, source_label: str) -> Card:
    """The card for a repository that could not be read: every number unknown, and why in words."""
    said = explain(code, vendor="GitHub")
    numbers = tuple(Metric(label=label, value=NotKnown(reason=code)) for label in LABELS)
    line = Entry(text=_fit(f"{said.what} {said.fix}"), tone=Tone.WARN)
    return _card(metrics.Freshness.NEVER, numbers, (line,), source_label=source_label, last_success_at=None)


def _build_line(health: RepoHealth) -> Entry:
    short = (health.build_commit or "")[:7]
    branch = _branch(health)
    if health.build is BuildState.PASSED:
        return Entry(
            text=f"Newest build on {branch} passed ({short})", tone=Tone.OK, at=health.build_finished_at
        )
    if health.build is BuildState.FAILED:
        many = "workflow" if health.failing_workflows == 1 else "workflows"
        text = f"Newest build on {branch} failed: {health.failing_workflows} {many} ({short})"
        return Entry(text=text, tone=Tone.ALERT, at=health.build_finished_at)
    if health.build is BuildState.INCONCLUSIVE:
        text = f"Latest builds on {branch} were cancelled or skipped: none passed ({short})"
        return Entry(text=text, tone=Tone.WARN, at=health.build_finished_at)
    if health.build is BuildState.RUNNING:
        return Entry(text=f"Newest build on {branch} is still running ({short})", tone=Tone.INFO)
    return Entry(text=f"No builds have run on {branch} yet", tone=Tone.INFO)


def _branch(health: RepoHealth) -> str:
    name = health.default_branch
    return name if len(name) <= _MAX_BRANCH else "the main branch"


def _card(
    fresh: metrics.Freshness,
    numbers: tuple[Metric, ...],
    lines: tuple[Entry, ...],
    *,
    source_label: str,
    last_success_at: datetime | None,
) -> Card:
    return Card(
        id=CARD_ID,
        title=TITLE,
        source=source_label,
        freshness=fresh,
        last_success_at=last_success_at,
        sample=False,
        metrics=numbers,
        entries=lines,
    )


def _fit(text: str) -> str:
    return text if len(text) <= _MAX_ENTRY else text[: _MAX_ENTRY - 1].rstrip() + "…"
