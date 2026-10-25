"""The tool gate (test IDs ``policy/deny_unlisted_tool``, ``policy/ceiling``, ``runner/step_limit``,
``runner/budget``, ``adv/taint_second_hop``, ``adv/exfil_*`` at the gate level)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agentzaaheen.core.policy import (
    AgentPolicy,
    AuditOutcome,
    Budget,
    Decision,
    DenyReason,
    Effect,
    KillSwitches,
    ProjectPolicy,
    RunState,
    Target,
    ToolCall,
    audit_draft,
    decide,
)
from agentzaaheen.core.sdk import DataClass, ToolLevel, ToolSpec, Trust

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
PROJECT = uuid4()
RUN = uuid4()

TOOLS = {
    "support.list_tickets": ToolSpec(
        name="support.list_tickets",
        level=ToolLevel.READ,
        output_data_class=DataClass.CUSTOMER_PII,
        description="List open tickets.",
    ),
    "support.get_ticket": ToolSpec(
        name="support.get_ticket",
        level=ToolLevel.READ,
        output_data_class=DataClass.CUSTOMER_PII,
        target_kind="ticket",
        description="Read one ticket.",
    ),
    "support.draft_reply": ToolSpec(
        name="support.draft_reply",
        target_kind="ticket",
        level=ToolLevel.DRAFT,
        output_data_class=DataClass.CUSTOMER_PII,
        description="Draft a reply for approval.",
    ),
    "email.send": ToolSpec(
        name="email.send",
        level=ToolLevel.ACT,
        output_data_class=DataClass.INTERNAL,
        target_kind="ticket",
        description="Send.",
    ),
    "web.search": ToolSpec(
        name="web.search",
        level=ToolLevel.READ,
        output_data_class=DataClass.PUBLIC,
        outbound=True,
        description="Search the web.",
    ),
    "repo.delete": ToolSpec(
        name="repo.delete", level=ToolLevel.FORBIDDEN, output_data_class=DataClass.INTERNAL, description="x"
    ),
    "metrics.mrr": ToolSpec(
        name="metrics.mrr",
        level=ToolLevel.READ,
        output_data_class=DataClass.INTERNAL,
        result_trust=Trust.TRUSTED,
        description="Monthly recurring revenue from our own database.",
    ),
}


class Checks:
    """A server-side lookup double: records calls, can be told to fail."""

    def __init__(self, *, args_error: str | None = None, owns: bool = True, explode: bool = False) -> None:
        self.args_error = args_error
        self.owns = owns
        self.explode = explode
        self.calls: list[str] = []

    def validate_args(self, spec: ToolSpec, args: Mapping[str, object]) -> str | None:
        self.calls.append(f"args:{spec.name}")
        if self.explode:
            raise RuntimeError("schema store unreachable")
        return self.args_error

    def owns_target(self, project_id: UUID, target: Target) -> bool:
        self.calls.append(f"owns:{target.kind}")
        return self.owns


def agent(**overrides: Any) -> AgentPolicy:
    fields: dict[str, Any] = {
        "agent": "support",
        "tools": frozenset(
            {"support.list_tickets", "support.get_ticket", "support.draft_reply", "web.search", "metrics.mrr"}
        ),
        "ceiling": ToolLevel.DRAFT,
        "budget": Budget(steps=10, tokens=20_000, seconds=300),
    }
    fields.update(overrides)
    return AgentPolicy.model_validate(fields)


def state(**overrides: Any) -> RunState:
    fields: dict[str, Any] = {"run_id": RUN, "agent": "support", "project_id": PROJECT, "started_at": NOW}
    fields.update(overrides)
    return RunState.model_validate(fields)


def call(tool: str, target: Target | None = None, **args: object) -> ToolCall:
    return ToolCall(run_id=RUN, tool=tool, args=args, target=target)


def run(  # test helper mirroring decide()'s inputs
    the_call: ToolCall,
    *,
    the_agent: AgentPolicy | None = None,
    project_ceiling: ToolLevel = ToolLevel.ACT,
    switches: KillSwitches | None = None,
    the_state: RunState | None = None,
    checks: Checks | None = None,
    now: datetime = NOW,
    spec: ToolSpec | None = None,
) -> Decision:
    return decide(
        the_call,
        spec=spec if spec is not None else TOOLS.get(the_call.tool),
        agent=the_agent or agent(),
        project=ProjectPolicy(project_id=PROJECT, ceiling=project_ceiling),
        switches=switches or KillSwitches(),
        state=the_state or state(),
        checks=checks or Checks(),
        now=now,
    )


TICKET = Target(kind="ticket", ref="t-42")


def test_read_tool_executes() -> None:
    decision = run(call("support.list_tickets"))
    assert decision.allowed
    assert decision.effect is Effect.EXECUTE


def test_draft_tool_only_proposes() -> None:
    decision = run(call("support.draft_reply", TICKET, body="Hello"))
    assert decision.allowed
    assert decision.effect is Effect.PROPOSE


def test_unlisted_tool_is_denied_even_if_it_exists() -> None:
    assert run(call("email.send", TICKET)).reason is DenyReason.TOOL_NOT_ALLOWED


def test_listed_but_undeclared_tool_is_denied() -> None:
    the_agent = agent(tools=frozenset({"ghost.tool"}))
    assert run(call("ghost.tool"), the_agent=the_agent).reason is DenyReason.UNKNOWN_TOOL


def test_spec_for_a_different_tool_is_denied() -> None:
    decision = run(call("support.list_tickets"), spec=TOOLS["metrics.mrr"])
    assert decision.reason is DenyReason.UNKNOWN_TOOL


def test_forbidden_tools_are_never_callable() -> None:
    the_agent = agent(tools=frozenset({"repo.delete"}), ceiling=ToolLevel.ACT)
    assert run(call("repo.delete"), the_agent=the_agent).reason is DenyReason.FORBIDDEN_TOOL


def test_agent_ceiling() -> None:
    the_agent = agent(tools=frozenset({"email.send"}), ceiling=ToolLevel.READ)
    assert run(call("email.send", TICKET), the_agent=the_agent).reason is DenyReason.ABOVE_AGENT_CEILING


def test_project_ceiling() -> None:
    decision = run(call("support.draft_reply", TICKET), project_ceiling=ToolLevel.READ)
    assert decision.reason is DenyReason.ABOVE_PROJECT_CEILING


def test_agent_ceiling_cannot_be_forbidden_and_tools_are_capped() -> None:
    with pytest.raises(ValidationError):
        agent(ceiling=ToolLevel.FORBIDDEN)
    with pytest.raises(ValidationError):
        agent(tools=frozenset(f"t.tool{i}" for i in range(7)))


@pytest.mark.parametrize(
    ("switches", "reason"),
    [
        (KillSwitches(stop_all=True), DenyReason.STOP_ALL),
        (KillSwitches(frozen_projects=frozenset({PROJECT})), DenyReason.PROJECT_FROZEN),
        (KillSwitches(paused_agents=frozenset({"support"})), DenyReason.AGENT_PAUSED),
    ],
)
def test_kill_switches_win_over_everything(switches: KillSwitches, reason: DenyReason) -> None:
    assert run(call("support.list_tickets"), switches=switches).reason is reason


@pytest.mark.parametrize(
    "mismatch",
    [{"run_id": uuid4()}, {"agent": "finance"}, {"project_id": uuid4()}],
)
def test_call_must_belong_to_this_run_agent_and_project(mismatch: dict[str, Any]) -> None:
    assert run(call("support.list_tickets"), the_state=state(**mismatch)).reason is DenyReason.RUN_MISMATCH


def test_split_run_rule_blocks_the_lethal_trifecta() -> None:
    tainted_private = state(trust=Trust.UNTRUSTED, data_class=DataClass.CUSTOMER_PII)
    decision = run(call("web.search", q="customer names"), the_state=tainted_private)
    assert decision.reason is DenyReason.SPLIT_RUN_REQUIRED


@pytest.mark.parametrize(
    ("trust", "data_class"),
    [(Trust.UNTRUSTED, DataClass.PUBLIC), (Trust.TRUSTED, DataClass.CUSTOMER_PII)],
)
def test_outbound_is_fine_without_the_full_trifecta(trust: Trust, data_class: DataClass) -> None:
    decision = run(call("web.search", q="pricing"), the_state=state(trust=trust, data_class=data_class))
    assert decision.allowed


def test_second_hop_reading_then_searching_is_blocked() -> None:
    # Step 1 reads customer tickets (untrusted, PII); step 2 tries to search the web.
    first = state()
    assert run(call("support.list_tickets"), the_state=first).allowed
    after = first.after_result(TOOLS["support.list_tickets"], tokens=500)
    assert after.trust is Trust.UNTRUSTED
    assert after.data_class is DataClass.CUSTOMER_PII
    assert run(call("web.search", q="x"), the_state=after).reason is DenyReason.SPLIT_RUN_REQUIRED


def test_trusted_internal_numbers_do_not_taint_the_run() -> None:
    after = state().after_result(TOOLS["metrics.mrr"], tokens=10)
    assert after.trust is Trust.TRUSTED
    assert after.data_class is DataClass.INTERNAL
    assert after.steps_used == 1


def test_taint_and_class_never_go_back_down() -> None:
    dirty = state().after_result(TOOLS["support.list_tickets"], tokens=0)
    cleaner = dirty.after_result(TOOLS["metrics.mrr"], tokens=-5)
    assert cleaner.trust is Trust.UNTRUSTED
    assert cleaner.data_class is DataClass.CUSTOMER_PII
    assert cleaner.tokens_used == 0


def test_draft_from_a_tainted_run_is_marked_for_the_approver() -> None:
    decision = run(call("support.draft_reply", TICKET, body="Hi"), the_state=state(trust=Trust.UNTRUSTED))
    assert decision.allowed
    assert decision.effect is Effect.PROPOSE
    assert decision.proposal_tainted


@pytest.mark.parametrize(
    ("overrides", "now", "reason"),
    [
        ({"steps_used": 10}, NOW, DenyReason.STEP_BUDGET),
        ({"tokens_used": 20_000}, NOW, DenyReason.TOKEN_BUDGET),
        ({}, NOW + timedelta(seconds=300), DenyReason.TIME_BUDGET),
    ],
)
def test_budgets(overrides: dict[str, Any], now: datetime, reason: DenyReason) -> None:
    assert run(call("support.list_tickets"), the_state=state(**overrides), now=now).reason is reason


def test_just_under_budget_is_allowed() -> None:
    almost = state(steps_used=9, tokens_used=19_999)
    assert run(call("support.list_tickets"), the_state=almost, now=NOW + timedelta(seconds=299)).allowed


def test_bad_args_are_denied_with_a_short_reason() -> None:
    decision = run(
        call("support.list_tickets", limit="lots"), checks=Checks(args_error="limit: not int" * 50)
    )
    assert decision.reason is DenyReason.BAD_ARGS
    assert decision.detail is not None
    assert len(decision.detail) <= 200


def test_draft_needs_a_target_and_it_must_be_ours() -> None:
    assert run(call("support.draft_reply", body="Hi")).reason is DenyReason.MISSING_TARGET
    decision = run(call("support.draft_reply", TICKET, body="Hi"), checks=Checks(owns=False))
    assert decision.reason is DenyReason.TARGET_NOT_OWNED


def test_read_with_a_target_is_ownership_checked_too() -> None:
    checks = Checks(owns=False)
    assert run(call("support.get_ticket", TICKET), checks=checks).reason is DenyReason.TARGET_NOT_OWNED
    assert "owns:ticket" in checks.calls


def test_a_failing_lookup_is_a_denial_not_a_crash() -> None:
    decision = run(call("support.list_tickets"), checks=Checks(explode=True))
    assert not decision.allowed
    assert decision.reason is DenyReason.CHECK_FAILED
    assert decision.detail == "_check_args: RuntimeError"


def test_a_truthy_but_not_true_ownership_answer_is_a_denial() -> None:
    class Sloppy(Checks):
        def owns_target(self, project_id: UUID, target: Target) -> bool:
            return "yes"  # type: ignore[return-value]  # proving only a real True counts

    assert run(call("support.get_ticket", TICKET), checks=Sloppy()).reason is DenyReason.TARGET_NOT_OWNED


def test_naive_clock_is_refused() -> None:
    naive = datetime(2026, 10, 17, 9, 0)  # noqa: DTZ001  # proving naive clocks are refused
    assert run(call("support.list_tickets"), now=naive).reason is DenyReason.CHECK_FAILED


def test_cheap_checks_run_before_server_lookups() -> None:
    checks = Checks()
    run(call("email.send", TICKET), checks=checks)
    assert checks.calls == []


def test_every_decision_becomes_an_audit_entry() -> None:
    correlation = uuid4()
    allowed = run(call("support.draft_reply", TICKET, body="Hi"), the_state=state(trust=Trust.UNTRUSTED))
    entry = audit_draft(
        allowed, call("support.draft_reply", TICKET), state(), now=NOW, correlation_id=correlation
    )
    assert entry.outcome is AuditOutcome.ALLOW
    assert entry.reason_code == "gate.allowed"
    assert entry.target_ref == "ticket:t-42"
    assert entry.details["effect"] == "propose"
    assert entry.details["tainted"] is True

    denied = run(call("email.send"))
    entry = audit_draft(denied, call("email.send"), state(), now=NOW, correlation_id=correlation)
    assert entry.outcome is AuditOutcome.DENY
    assert entry.reason_code == "gate.tool_not_allowed"
    assert entry.target_ref is None
    assert "effect" not in entry.details


def test_audit_never_stores_a_personal_target() -> None:
    to_person = Target(kind="contact", ref="jane.doe@example.com")
    decision = run(call("support.get_ticket", to_person))
    entry = audit_draft(
        decision, call("support.get_ticket", to_person), state(), now=NOW, correlation_id=uuid4()
    )
    assert entry.target_ref == "contact:[withheld]"


LADDER = [ToolLevel.READ, ToolLevel.DRAFT, ToolLevel.ACT]
LEVEL_TOOL = {
    ToolLevel.READ: "support.get_ticket",
    ToolLevel.DRAFT: "support.draft_reply",
    ToolLevel.ACT: "email.send",
}


@pytest.mark.parametrize("ceiling", LADDER)
@pytest.mark.parametrize("level", LADDER)
def test_agent_ceiling_at_every_step_of_the_ladder(ceiling: ToolLevel, level: ToolLevel) -> None:
    the_agent = agent(tools=frozenset(LEVEL_TOOL.values()), ceiling=ceiling)
    decision = run(call(LEVEL_TOOL[level], TICKET), the_agent=the_agent)
    assert decision.allowed == (level.rank <= ceiling.rank)
    if not decision.allowed:
        assert decision.reason is DenyReason.ABOVE_AGENT_CEILING


@pytest.mark.parametrize("ceiling", LADDER)
@pytest.mark.parametrize("level", LADDER)
def test_project_ceiling_at_every_step_of_the_ladder(ceiling: ToolLevel, level: ToolLevel) -> None:
    the_agent = agent(tools=frozenset(LEVEL_TOOL.values()), ceiling=ToolLevel.ACT)
    decision = run(call(LEVEL_TOOL[level], TICKET), the_agent=the_agent, project_ceiling=ceiling)
    assert decision.allowed == (level.rank <= ceiling.rank)
    if not decision.allowed:
        assert decision.reason is DenyReason.ABOVE_PROJECT_CEILING


def test_object_tools_take_their_object_only_as_a_checked_target() -> None:
    # A read tool that addresses one object cannot be called without the gate-checked target.
    assert run(call("support.get_ticket", id="other-projects-ticket")).reason is DenyReason.MISSING_TARGET


def test_target_of_the_wrong_kind_is_denied_before_any_lookup() -> None:
    checks = Checks()
    decision = run(call("support.get_ticket", Target(kind="invoice", ref="i-1")), checks=checks)
    assert decision.reason is DenyReason.WRONG_TARGET_KIND
    assert not any(c.startswith("owns:") for c in checks.calls)


def test_tools_without_a_target_kind_refuse_a_target() -> None:
    decision = run(call("support.list_tickets", TICKET))
    assert decision.reason is DenyReason.UNEXPECTED_TARGET


def test_write_tools_must_declare_a_target_kind() -> None:
    with pytest.raises(ValidationError):
        ToolSpec(name="x.draft", level=ToolLevel.DRAFT, output_data_class=DataClass.PUBLIC, description="d")


def test_clock_going_backwards_is_a_denial_not_unlimited_time() -> None:
    decision = run(call("support.list_tickets"), now=NOW - timedelta(seconds=1))
    assert decision.reason is DenyReason.CLOCK_WENT_BACKWARDS
