"""The dashboard's cards as plain text, for the chat model to answer from (F05).

The model sees exactly what the person sees on screen and nothing more: the
same numbers, the same "unknown"s with their reasons, the same sample tags.
Money is written from integer minor units with the currency's own number of
decimals, so the model never has to do that arithmetic itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from agentzaaheen.core.api.views import (
    Card,
    Count,
    Entry,
    Metric,
    Money,
    Percent,
    ProjectDetail,
    ProjectSummary,
)

# ISO 4217 currencies whose minor unit is not hundredths.
_DECIMALS: dict[str, int] = {
    **dict.fromkeys(("BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX"), 0),
    **dict.fromkeys(("UYI", "VND", "VUV", "XAF", "XOF", "XPF"), 0),
    **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
}


def money_text(money: Money) -> str:
    """``USD 1240.00`` from 124000 minor units; ``JPY 500`` from 500."""
    places = _DECIMALS.get(money.currency, 2)
    amount = Decimal(money.minor).scaleb(-places)
    return f"{money.currency} {amount:.{places}f}"


def _value(metric: Metric) -> str:
    value = metric.value
    if isinstance(value, Money):
        text = money_text(value)
    elif isinstance(value, Count):
        text = str(value.value)
    elif isinstance(value, Percent):
        text = f"{Decimal(value.basis_points).scaleb(-2):.2f}%"
    else:
        text = f"unknown ({value.reason})"
    return f"{text}; {metric.detail}" if metric.detail else text


def _entry(entry: Entry) -> str:
    when = f" [{entry.at.isoformat()}]" if entry.at is not None else ""
    return f"  * ({entry.tone.value}){when} {entry.text}"


def _card(card: Card) -> list[str]:
    seen = card.last_success_at.isoformat() if card.last_success_at is not None else "never"
    tag = "SAMPLE (made-up numbers)" if card.sample else "real"
    lines = [
        f"Card: {card.title} (source {card.source}; {tag}; data {card.freshness.value}; last read {seen})"
    ]
    lines += [f"  - {m.label}: {_value(m)}" for m in card.metrics]
    lines += [_entry(e) for e in card.entries]
    return lines


def _project_line(project: ProjectSummary) -> str:
    tag = "; some SAMPLE data" if project.sample else ""
    return (
        f"Project: {project.name} ({project.summary}); health {project.health.value}; "
        f"{project.escalations} escalated for review{tag}"
    )


def project_facts(detail: ProjectDetail) -> str:
    """One project's cards, as the chat model's only source of facts."""
    lines = [_project_line(detail.project)]
    for card in detail.cards:
        lines += _card(card)
    return "\n".join(lines)


def portfolio_facts(projects: Sequence[ProjectSummary]) -> str:
    """The project list, for a question asked with no project open."""
    return "\n".join(_project_line(p) for p in projects) or "No projects."


def uses_sample(detail: ProjectDetail) -> bool:
    """Whether any card behind these facts is sample data."""
    return detail.project.sample or any(card.sample for card in detail.cards)
