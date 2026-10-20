"""Sample data for the dashboard's first version (F04 slice 1).

Every card says ``sample=True`` and the UI labels it, so a sample number can
never be mistaken for a real one. Times are relative to ``now`` so the
freshness labels read sensibly whenever the dashboard is opened. Slice 2
replaces this with real numbers from the stored snapshots.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from yaqzan.core.api.views import (
    Card,
    Count,
    Entry,
    Health,
    Metric,
    ModelChoice,
    ModelList,
    Money,
    NotKnown,
    Percent,
    ProjectDetail,
    ProjectSummary,
    Tone,
)
from yaqzan.core.metrics import Freshness

_NOT_CONNECTED = NotKnown(reason="source.not_connected")


class SamplePortfolio:
    """Four products with made-up but realistic numbers."""

    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        self._now = now
        self._details = {d.project.id: d for d in self._build()}

    def projects(self) -> tuple[ProjectSummary, ...]:
        """The list on the left, in display order."""
        return tuple(d.project for d in self._details.values())

    def project(self, project_id: str) -> ProjectDetail | None:
        """One project's cards, or ``None`` if there is no such project."""
        return self._details.get(project_id)

    def models(self) -> ModelList:
        """The chat's model menu (ADR-023). The dashboard marks the connected one available."""
        return ModelList(
            models=(
                ModelChoice(
                    id="local-gemma",
                    label="Gemma (local)",
                    where="local",
                    available=False,
                    default=True,
                    note="Free and open. Runs on this laptop for now, on our own server later.",
                ),
                ModelChoice(
                    id="cloud-claude",
                    label="Claude (cloud)",
                    where="cloud",
                    available=False,
                    default=False,
                    note="Opt-in. Never sees customer data. Needs a key in the vault.",
                ),
                ModelChoice(
                    id="cloud-gpt",
                    label="GPT (cloud)",
                    where="cloud",
                    available=False,
                    default=False,
                    note="Opt-in. Never sees customer data. Needs a key in the vault.",
                ),
            )
        )

    def _ago(self, **delta: float) -> datetime:
        return self._now - timedelta(**delta)

    def _fresh(self, card_id: str, title: str, source: str, minutes: float, **rest: object) -> Card:
        return Card.model_validate(
            {
                "id": card_id,
                "title": title,
                "source": source,
                "freshness": Freshness.FRESH,
                "last_success_at": self._ago(minutes=minutes),
                "sample": True,
                **rest,
            }
        )

    def _not_connected(self, card_id: str, title: str, source: str, labels: tuple[str, ...]) -> Card:
        return Card(
            id=card_id,
            title=title,
            source=source,
            freshness=Freshness.NEVER,
            sample=True,
            metrics=tuple(Metric(label=label, value=_NOT_CONNECTED) for label in labels),
            entries=(Entry(text=f"{source} is not connected yet.", tone=Tone.INFO),),
        )

    def _build(self) -> tuple[ProjectDetail, ...]:
        return (self._zaaheen(), self._sandoq_kin(), self._agent_x(), self._eva())

    def _zaaheen(self) -> ProjectDetail:
        usd = "USD"
        return ProjectDetail(
            project=ProjectSummary(
                id="zaaheen",
                name="Zaaheen",
                summary="Memory Vault desktop app",
                health=Health.WARN,
                escalations=2,
                sample=True,
            ),
            cards=(
                self._fresh(
                    "paddle",
                    "Revenue",
                    "Paddle",
                    12,
                    metrics=(
                        Metric(label="MRR", value=Money(minor=124_000, currency=usd)),
                        Metric(label="Active subscribers", value=Count(value=87)),
                        Metric(label="Trials", value=Count(value=14)),
                        Metric(
                            label="Failed payments",
                            value=Count(value=2),
                            detail="Last 7 days, USD 58.00",
                        ),
                        Metric(label="Refunds", value=Count(value=1), detail="Last 30 days, USD 19.00"),
                    ),
                ),
                self._fresh(
                    "stripe",
                    "Revenue",
                    "Stripe",
                    25,
                    metrics=(
                        Metric(label="MRR", value=Money(minor=31_500, currency=usd)),
                        Metric(label="Active subscribers", value=Count(value=21)),
                        Metric(label="Failed payments", value=Count(value=0), detail="Last 7 days"),
                    ),
                ),
                self._fresh(
                    "clients",
                    "Clients",
                    "Paddle + Stripe",
                    12,
                    metrics=(
                        Metric(label="Signed up", value=Count(value=412)),
                        Metric(label="New this week", value=Count(value=23)),
                        Metric(label="Cancelled", value=Count(value=3), detail="Last 30 days"),
                    ),
                ),
                self._fresh(
                    "support",
                    "Support email",
                    "Support inbox",
                    8,
                    metrics=(
                        Metric(label="Responded", value=Count(value=23), detail="Yesterday"),
                        Metric(label="Escalated to you", value=Count(value=2)),
                        Metric(label="Waiting", value=Count(value=1)),
                    ),
                    entries=(
                        Entry(
                            text="Escalated: a customer asks for a refund. Money always comes to you.",
                            tone=Tone.ALERT,
                            at=self._ago(hours=3),
                        ),
                        Entry(
                            text="Escalated: an email tried to give the AI instructions.",
                            tone=Tone.WARN,
                            at=self._ago(hours=9),
                        ),
                        Entry(
                            text="Autopilot is in practice mode: it drafts, it does not send (ADR-021).",
                            tone=Tone.INFO,
                        ),
                    ),
                ),
                self._fresh(
                    "github",
                    "Open issues",
                    "GitHub",
                    30,
                    metrics=(
                        Metric(label="Open issues", value=Count(value=5)),
                        Metric(label="Open pull requests", value=Count(value=2)),
                        Metric(label="Failing builds", value=Count(value=0)),
                    ),
                    entries=(
                        Entry(
                            text="New issue: sync stops after laptop sleep",
                            tone=Tone.WARN,
                            at=self._ago(hours=5),
                        ),
                        Entry(
                            text="Security alert closed: dependency updated",
                            tone=Tone.OK,
                            at=self._ago(days=1),
                        ),
                    ),
                ),
                self._not_connected("uptime", "Uptime", "Uptime checker", ("Uptime (30 days)", "Incidents")),
                self._fresh(
                    "activity",
                    "Yesterday's agent activity",
                    "Audit log",
                    5,
                    entries=(
                        Entry(
                            text="Revenue checked 24 times, all fine", tone=Tone.OK, at=self._ago(hours=14)
                        ),
                        Entry(
                            text="Support agent drafted 25 replies, 2 escalated",
                            tone=Tone.INFO,
                            at=self._ago(hours=16),
                        ),
                        Entry(
                            text="Key check: Paddle key expires 16 Jan 2027",
                            tone=Tone.INFO,
                            at=self._ago(hours=18),
                        ),
                        Entry(text="Morning briefing sent at 06:30", tone=Tone.OK, at=self._ago(hours=20)),
                    ),
                ),
            ),
        )

    def _sandoq_kin(self) -> ProjectDetail:
        return ProjectDetail(
            project=ProjectSummary(
                id="sandoq-kin",
                name="Sandoq Kin",
                summary="Family mobile app",
                health=Health.OK,
                escalations=0,
                sample=True,
            ),
            cards=(
                self._fresh(
                    "revenuecat",
                    "Revenue",
                    "RevenueCat",
                    40,
                    metrics=(
                        Metric(label="MRR", value=Money(minor=48_900, currency="USD")),
                        Metric(label="Active subscribers", value=Count(value=63)),
                        Metric(label="Trials", value=Count(value=9)),
                    ),
                ),
                self._fresh(
                    "reviews",
                    "Store reviews",
                    "App Store + Play",
                    55,
                    metrics=(
                        Metric(label="New reviews", value=Count(value=4), detail="Last 7 days"),
                        Metric(label="Below 3 stars", value=Count(value=0)),
                    ),
                ),
                self._not_connected("uptime", "Uptime", "Uptime checker", ("Uptime (30 days)",)),
            ),
        )

    def _agent_x(self) -> ProjectDetail:
        return ProjectDetail(
            project=ProjectSummary(
                id="agent-x",
                name="Agent X",
                summary="Payments platform (our own ops data only)",
                health=Health.OK,
                escalations=0,
                sample=True,
            ),
            cards=(
                self._fresh(
                    "uptime",
                    "Uptime",
                    "Uptime checker",
                    3,
                    metrics=(
                        Metric(label="Uptime (30 days)", value=Percent(basis_points=9_995)),
                        Metric(label="Incidents", value=Count(value=1), detail="4 minutes, 3 days ago"),
                    ),
                ),
                self._fresh(
                    "github",
                    "Open issues",
                    "GitHub",
                    30,
                    metrics=(
                        Metric(label="Open issues", value=Count(value=11)),
                        Metric(label="Failing builds", value=Count(value=0)),
                    ),
                ),
            ),
        )

    def _eva(self) -> ProjectDetail:
        return ProjectDetail(
            project=ProjectSummary(
                id="eva",
                name="Eva",
                summary="AI agent SaaS",
                health=Health.UNKNOWN,
                escalations=0,
                sample=True,
            ),
            cards=(
                self._not_connected("paddle", "Revenue", "Paddle", ("MRR", "Active subscribers")),
                self._not_connected("uptime", "Uptime", "Uptime checker", ("Uptime (30 days)",)),
            ),
        )
