"""When an uptime change is worth a phone alert, and what it says (ADR-031).

Pure rules, no network: :class:`UptimeWatch` is fed each round of checks and
returns the alerts to send. An address must fail ``threshold`` checks in a
row before a "down" alert; one "back up" alert follows when it answers again;
nothing repeats while it stays down. Addresses already down at the first
round are reported once, so the Chairman learns of them.

Every word of an alert comes from this module's fixed template: the project's
display name, the parts' own short names (``api``, ``sign-in``: ours, from the
uptime list), counts, and our own words for each reason. Nothing a website,
log or person wrote, and never a full address (ADR-031).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from yaqzan.core.sdk import UptimeReport

DEFAULT_THRESHOLD = 2
# Our own words for each reason code; anything else is "not as expected".
REASON_WORDS: Mapping[str, str] = {
    "uptime.no_answer": "did not answer",
    "uptime.server_error": "server error",
    "uptime.not_found": "page not found",
    "uptime.refused": "refused the request",
    "uptime.wrong_answer": "answered as something else",
}


@dataclass(frozen=True)
class Alert:
    """One phone alert: built only from the fixed template."""

    project_id: str
    down: bool
    title: str
    message: str
    priority: str


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}es"


@dataclass
class UptimeWatch:
    """Remembers each address between rounds; ``observe`` says which changes need an alert."""

    names: Mapping[str, str]
    threshold: int = DEFAULT_THRESHOLD
    _fails: dict[tuple[str, str], int] = field(default_factory=dict)
    _alerted: set[tuple[str, str]] = field(default_factory=set)
    _rounds: int = 0

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("the threshold must be at least one check")

    def observe(self, reports: Mapping[str, UptimeReport]) -> list[Alert]:
        """The alerts this round of checks calls for (at most one down and one back-up per project)."""
        first = self._rounds == 0
        self._rounds += 1
        alerts: list[Alert] = []
        for project, report in sorted(reports.items()):
            newly_down: list[tuple[str | None, str]] = []
            back_up: list[str | None] = []
            for site in report.sites:
                key = (project, site.url)
                if site.up:
                    self._fails.pop(key, None)
                    if key in self._alerted:
                        self._alerted.discard(key)
                        back_up.append(site.part)
                    continue
                self._fails[key] = self._fails.get(key, 0) + 1
                if key not in self._alerted and (first or self._fails[key] >= self.threshold):
                    self._alerted.add(key)
                    newly_down.append((site.part, REASON_WORDS.get(site.reason, "not as expected")))
            if newly_down:
                alerts.append(self._down(project, report, newly_down))
            if back_up:
                alerts.append(self._up(project, report, back_up))
        return alerts

    def _name(self, project: str) -> str:
        return self.names.get(project, project)

    def _down(self, project: str, report: UptimeReport, newly: list[tuple[str | None, str]]) -> Alert:
        down = len(report.sites) - report.up
        why = ", ".join(sorted({reason for _, reason in newly}))
        counted = f"{down} of {_plural(len(report.sites), 'address')} down"
        parts = _parts([part for part, _ in newly])
        what = f"{parts} down ({why}); {counted}" if parts else f"{counted} ({why})"
        return Alert(
            project_id=project,
            down=True,
            title=f"Agent Zaaheen: {self._name(project)} is down",
            message=f"{what}. Details on the dashboard.",
            priority="high",
        )

    def _up(self, project: str, report: UptimeReport, recovered: list[str | None]) -> Alert:
        parts = _parts(recovered)
        which = parts if parts else _plural(len(recovered), "address")
        return Alert(
            project_id=project,
            down=False,
            title=f"Agent Zaaheen: {self._name(project)} is back up",
            message=f"{which} answering again; {report.up} of {len(report.sites)} up now.",
            priority="default",
        )


def _parts(parts: list[str | None]) -> str:
    """The parts' names joined, or "" when any address has no part (then counts are used instead)."""
    if not parts or any(part is None for part in parts):
        return ""
    return ", ".join(sorted({part for part in parts if part is not None}))
