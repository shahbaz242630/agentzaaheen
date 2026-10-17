from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from yaqzan.core.sdk import (
    Action,
    ActionLevel,
    ActionStatus,
    DataClass,
    Event,
    Evidence,
    Finding,
    FindingKind,
    ModelInfo,
    RunOutcome,
    RunRecord,
    Severity,
    Trust,
    combine_trust,
    make_idempotency_key,
    max_data_class,
)

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
SHA = "a" * 64


def event(**overrides: Any) -> Event:
    fields: dict[str, Any] = {
        "tenant_id": uuid4(),
        "project_id": uuid4(),
        "type": "ticket.created",
        "source": "imap",
        "idempotency_key": "imap:123",
        "occurred_at": NOW,
        "received_at": NOW,
        "schema_version": 1,
        "payload": {"subject": "hello"},
        "trust": Trust.UNTRUSTED,
        "data_class": DataClass.CUSTOMER_PII,
    }
    fields.update(overrides)
    return Event(**fields)


def action(**overrides: Any) -> Action:
    fields: dict[str, Any] = {
        "tenant_id": uuid4(),
        "project_id": uuid4(),
        "run_id": uuid4(),
        "tool": "email.send",
        "level": ActionLevel.ACT,
        "payload": {"body": "Thanks!"},
        "target_kind": "email_thread",
        "target_ref": "thread-1",
        "data_classes": (DataClass.CUSTOMER_PII,),
        "leak_check_passed": True,
        "idempotency_key": "support:evt-1:email.send:thread-1",
    }
    fields.update(overrides)
    return Action(**fields)


def run_record(**overrides: Any) -> RunRecord:
    fields: dict[str, Any] = {
        "tenant_id": uuid4(),
        "project_id": uuid4(),
        "agent": "support",
        "agent_version": "1.0.0",
        "prompt_hash": SHA,
        "data_class_max": DataClass.INTERNAL,
        "trust": Trust.TRUSTED,
        "steps": 3,
        "tokens_in": 100,
        "tokens_out": 50,
        "cost_minor_units": 0,
        "started_at": NOW,
        "duration_ms": 1200,
        "outcome": RunOutcome.OK,
    }
    fields.update(overrides)
    return RunRecord(**fields)


# ---------------------------------------------------------------- data class and taint


def test_data_class_only_goes_up() -> None:
    assert max_data_class(DataClass.PUBLIC, DataClass.CUSTOMER_PII, DataClass.INTERNAL) is (
        DataClass.CUSTOMER_PII
    )


def test_data_class_of_nothing_is_public() -> None:
    assert max_data_class() is DataClass.PUBLIC


@given(st.lists(st.sampled_from(list(DataClass)), min_size=1))
def test_max_data_class_is_never_below_any_input(classes: list[DataClass]) -> None:
    result = max_data_class(*classes)
    assert all(result.rank >= c.rank for c in classes)


def test_taint_spreads() -> None:
    assert combine_trust(Trust.TRUSTED, Trust.UNTRUSTED) is Trust.UNTRUSTED
    assert combine_trust(Trust.TRUSTED, Trust.TRUSTED) is Trust.TRUSTED
    assert combine_trust() is Trust.TRUSTED


# ---------------------------------------------------------------- idempotency


def test_idempotency_key_uses_vendor_id() -> None:
    assert make_idempotency_key("paddle", "evt_1") == "paddle:evt_1"


def test_idempotency_key_falls_back_to_content_hash_and_is_stable() -> None:
    first = make_idempotency_key("gatus", None, "down@09:00")
    assert first == make_idempotency_key("gatus", None, "down@09:00")
    assert first != make_idempotency_key("gatus", None, "down@09:01")
    assert first.startswith("gatus:sha256:")


def test_idempotency_key_needs_a_source() -> None:
    with pytest.raises(ValueError, match="source"):
        make_idempotency_key("", "evt_1")


# ---------------------------------------------------------------- events


def test_valid_event() -> None:
    assert event().type == "ticket.created"


@pytest.mark.parametrize("bad_type", ["Ticket.Created", "ticket", "ticket..created", "1ticket.created"])
def test_event_type_must_be_dotted_lowercase(bad_type: str) -> None:
    with pytest.raises(ValidationError):
        event(type=bad_type)


def test_event_times_must_carry_a_timezone() -> None:
    with pytest.raises(ValidationError):
        event(occurred_at=datetime(2026, 10, 17, 9, 0))  # noqa: DTZ001 - naive on purpose


def test_event_from_far_future_is_rejected() -> None:
    with pytest.raises(ValidationError, match="more than a day"):
        event(occurred_at=NOW + timedelta(days=2))


def test_event_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        event(surprise="x")


def test_records_are_immutable() -> None:
    e = event()
    with pytest.raises(ValidationError):
        e.trust = Trust.TRUSTED  # type: ignore[misc]  # proving immutability


# ---------------------------------------------------------------- findings and evidence


def test_finding_needs_evidence() -> None:
    with pytest.raises(ValidationError):
        Finding(
            tenant_id=uuid4(),
            project_id=uuid4(),
            kind=FindingKind.INCIDENT,
            severity=Severity.P1,
            title="Site down",
            body="",
            evidence=(),
            trust=Trust.TRUSTED,
            data_class=DataClass.INTERNAL,
        )


def test_finding_with_evidence() -> None:
    finding = Finding(
        tenant_id=uuid4(),
        project_id=uuid4(),
        kind=FindingKind.INCIDENT,
        severity=Severity.P1,
        title="Site down",
        body="zaaheen.com failed 3 checks",
        evidence=(Evidence(metric_id="uptime.zaaheen"),),
        trust=Trust.TRUSTED,
        data_class=DataClass.INTERNAL,
    )
    assert finding.status == "open"


def test_evidence_must_reference_exactly_one_thing() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        Evidence()
    with pytest.raises(ValidationError, match="exactly one"):
        Evidence(metric_id="m", url="https://example.com")


def test_evidence_urls_must_be_https() -> None:
    with pytest.raises(ValidationError, match="https"):
        Evidence(url="http://example.com")


# ---------------------------------------------------------------- actions: the human gate


def test_proposed_action_is_valid() -> None:
    assert action().status is ActionStatus.PROPOSED


def test_approved_action_needs_an_approver() -> None:
    with pytest.raises(ValidationError, match="requires approved_by"):
        action(status=ActionStatus.APPROVED)


def test_sent_action_with_approval_is_valid() -> None:
    sent = action(status=ActionStatus.SENT, approved_by=uuid4(), approved_at=NOW)
    assert sent.status is ActionStatus.SENT


def test_action_failing_leak_check_can_never_be_approved() -> None:
    with pytest.raises(ValidationError, match="leak check"):
        action(
            status=ActionStatus.APPROVED,
            approved_by=uuid4(),
            approved_at=NOW,
            leak_check_passed=False,
        )


def test_proposed_action_cannot_carry_an_approval() -> None:
    with pytest.raises(ValidationError, match="cannot carry"):
        action(approved_by=uuid4(), approved_at=NOW)


# ---------------------------------------------------------------- run records


def test_run_record_ok() -> None:
    assert run_record().outcome is RunOutcome.OK


@pytest.mark.parametrize("outcome", [RunOutcome.COULD_NOT_RUN, RunOutcome.BUDGET_EXCEEDED, RunOutcome.DENIED])
def test_failed_run_needs_an_error_code(outcome: RunOutcome) -> None:
    with pytest.raises(ValidationError, match="error_code"):
        run_record(outcome=outcome)
    assert run_record(outcome=outcome, error_code="vendor_timeout").error_code == "vendor_timeout"


def test_nothing_found_is_not_a_failure() -> None:
    assert run_record(outcome=RunOutcome.NOTHING_FOUND).error_code is None


def test_model_info_requires_real_sha() -> None:
    assert ModelInfo(tier=1, model_id="gemma-4-e4b", file_sha256=SHA, provider="local").tier == 1
    with pytest.raises(ValidationError):
        ModelInfo(tier=1, model_id="x", file_sha256="not-a-sha", provider="local")
    with pytest.raises(ValidationError):
        ModelInfo(tier=4, model_id="x", provider="local")
