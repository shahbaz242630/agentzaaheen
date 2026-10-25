"""Who may do what, from where (test IDs ``mcp/ai_operator_cannot_approve``,
``notify/cannot_approve``, ``auth/stepup_required``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentzaaheen.core.policy import (
    INCREASES_RISK,
    REDUCES_RISK,
    STEP_UP_MAX_AGE,
    Channel,
    Operation,
    Role,
    authorize,
)

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
FRESH = NOW - timedelta(minutes=5)


def test_risk_sets_cover_every_operation_exactly_once() -> None:
    assert set(Operation) == REDUCES_RISK | INCREASES_RISK
    assert not REDUCES_RISK & INCREASES_RISK
    assert Operation.APPROVE_ACTION in INCREASES_RISK
    assert Operation.STOP_ALL in REDUCES_RISK


@pytest.mark.parametrize("operation", list(Operation))
@pytest.mark.parametrize("channel", list(Channel))
def test_ai_operator_only_ever_reads_over_mcp(operation: Operation, channel: Channel) -> None:
    decision = authorize(Role.AI_OPERATOR, operation, channel, step_up_at=FRESH, now=NOW)
    assert decision.allowed == (operation is Operation.VIEW and channel is Channel.MCP)


@pytest.mark.parametrize("operation", list(Operation))
def test_viewer_only_reads_on_the_dashboard(operation: Operation) -> None:
    decision = authorize(Role.VIEWER, operation, Channel.DASHBOARD, step_up_at=FRESH, now=NOW)
    assert decision.allowed == (operation is Operation.VIEW)
    assert not authorize(Role.VIEWER, Operation.VIEW, Channel.TELEGRAM, step_up_at=None, now=NOW).allowed


@pytest.mark.parametrize("operation", list(Operation))
def test_agent_auditor_can_only_pause(operation: Operation) -> None:
    decision = authorize(Role.AGENT_AUDITOR, operation, Channel.SYSTEM, step_up_at=None, now=NOW)
    assert decision.allowed == (operation is Operation.PAUSE_AGENT)
    assert not authorize(
        Role.AGENT_AUDITOR, Operation.PAUSE_AGENT, Channel.TELEGRAM, step_up_at=None, now=NOW
    ).allowed


@pytest.mark.parametrize("operation", sorted(REDUCES_RISK))
@pytest.mark.parametrize("channel", [Channel.DASHBOARD, Channel.TELEGRAM, Channel.VOICE, Channel.NTFY])
def test_chairman_can_reduce_risk_from_any_channel(operation: Operation, channel: Channel) -> None:
    assert authorize(Role.CHAIRMAN, operation, channel, step_up_at=None, now=NOW).allowed


@pytest.mark.parametrize("operation", sorted(INCREASES_RISK))
@pytest.mark.parametrize("channel", [Channel.TELEGRAM, Channel.VOICE, Channel.NTFY])
def test_nothing_risky_from_phone_or_voice_even_with_a_passkey(
    operation: Operation, channel: Channel
) -> None:
    decision = authorize(Role.CHAIRMAN, operation, channel, step_up_at=FRESH, now=NOW)
    assert not decision.allowed
    assert decision.reason == "access.needs_dashboard"


@pytest.mark.parametrize("operation", sorted(INCREASES_RISK))
def test_risky_operations_need_a_fresh_passkey(operation: Operation) -> None:
    assert authorize(Role.CHAIRMAN, operation, Channel.DASHBOARD, step_up_at=FRESH, now=NOW).allowed
    stale = NOW - STEP_UP_MAX_AGE - timedelta(seconds=1)
    for step_up in (None, stale, NOW + timedelta(seconds=1)):
        decision = authorize(Role.CHAIRMAN, operation, Channel.DASHBOARD, step_up_at=step_up, now=NOW)
        assert not decision.allowed
        assert decision.reason == "access.needs_fresh_passkey"


def test_step_up_exactly_at_the_limit_still_counts() -> None:
    edge = NOW - STEP_UP_MAX_AGE
    assert authorize(
        Role.CHAIRMAN, Operation.APPROVE_ACTION, Channel.DASHBOARD, step_up_at=edge, now=NOW
    ).allowed


def test_naive_times_never_count_as_a_step_up() -> None:
    naive = datetime(2026, 10, 17, 8, 59)  # noqa: DTZ001  # proving naive times are refused
    assert not authorize(
        Role.CHAIRMAN, Operation.APPROVE_ACTION, Channel.DASHBOARD, step_up_at=naive, now=NOW
    ).allowed


@pytest.mark.parametrize("channel", [Channel.MCP, Channel.SYSTEM])
def test_chairman_does_not_act_through_machine_channels(channel: Channel) -> None:
    decision = authorize(Role.CHAIRMAN, Operation.STOP_ALL, channel, step_up_at=FRESH, now=NOW)
    assert not decision.allowed


@pytest.mark.parametrize("role", ["viewer", "chairman", "admin", None])
def test_anything_that_is_not_a_real_role_is_denied(role: object) -> None:
    decision = authorize(
        role,  # type: ignore[arg-type]  # proving raw values from a DB or token fail closed
        Operation.APPROVE_ACTION,
        Channel.DASHBOARD,
        step_up_at=FRESH,
        now=NOW,
    )
    assert not decision.allowed
    assert decision.reason == "access.unknown_role"
