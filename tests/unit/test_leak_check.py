"""Before anything leaves a run (ADR-036 checkpoint 4, F11 slice 3): secrets never kept, customer data tagged.

Findings: a secret holds the finding back; customer details it did not declare move it up to customer data
(never down; an untrusted finding stays untrusted). Proposals: only :func:`checked_action` decides
``leak_check_passed``; a secret, customer details or no name list fails it, and a failed one can never be
approved.
"""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from agentzaaheen.core.llm_gateway import KnownNames
from agentzaaheen.core.runner import FindingDraft, check_findings, checked_action, leak_check
from agentzaaheen.core.sdk import (
    Action,
    ActionLevel,
    ActionStatus,
    DataClass,
    Evidence,
    FindingKind,
    Severity,
    Trust,
)

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "agentzaaheen"
TENANT = UUID("00000000-0000-4000-8000-0000000000d1")
EVA = UUID("00000000-0000-4000-8000-0000000000d2")
RUN = UUID("00000000-0000-4000-8000-0000000000d3")
TOKEN = "ghp_" + "Q" * 36  # built here: the shape of a GitHub token, never a real one
NAMES = KnownNames.of(["Fatima Al Mansoori", "Grace"], lambda text: text.encode())


def draft(
    body: str = "Eva answered 503 three times.",
    *,
    data_class: DataClass = DataClass.PUBLIC,
    trust: Trust = Trust.TRUSTED,
    url: str | None = None,
) -> FindingDraft:
    evidence = Evidence(url=url) if url is not None else Evidence(metric_id="uptime.eva")
    return FindingDraft(
        kind=FindingKind.INCIDENT,
        severity=Severity.P2,
        title="Eva is down",
        body=body,
        evidence=(evidence,),
        trust=trust,
        data_class=data_class,
    )


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("Revenue is up 12% this month.", ()),
        (f"the build log printed {TOKEN}", ("secret",)),
        ("-----BEGIN RSA PRIVATE KEY-----\nabc", ("secret",)),
        ("see https://admin:hunter2@example.com/x", ("email", "secret")),  # it looks like an email too
        ("write to fatima@example.com", ("email",)),
        ("call +971 50 123 4567", ("phone",)),
        ("customer ctm_01h8441jn5pcwrfhwh78jqt8hk asked", ("customer_id",)),
        ("Fatima Al Mansoori cancelled", ("known_customer_name",)),
        ("Grace cancelled", ("known_customer_single_name",)),
        ("a 7-day grace period", ()),  # a one-word name only counts written like a name
        (f"{TOKEN} for fatima@example.com", ("email", "secret")),
    ],
)
def test_the_check_names_kinds_never_the_text(text: str, hits: tuple[str, ...]) -> None:
    found = leak_check([text], NAMES)
    assert found.hits == hits
    assert found.passed is (not hits)
    assert TOKEN not in repr(found)
    assert "fatima" not in repr(found)


def test_text_too_long_to_check_never_passes() -> None:
    found = leak_check(["a" * 2_000_001], NAMES)
    assert (found.passed, found.hits) == (False, ("too_long",))


def test_a_finding_with_a_secret_is_held_back_and_the_rest_are_kept() -> None:
    clean, leaky = draft(), draft(f"the log said token={TOKEN}")
    checked = check_findings([clean, leaky], NAMES)
    assert checked.kept == (clean,)
    assert (checked.held_back, checked.retagged) == (1, 0)


def test_a_secret_in_an_evidence_address_is_found_too() -> None:
    checked = check_findings([draft(url=f"https://github.com/x?token={TOKEN}")], NAMES)
    assert (checked.kept, checked.held_back) == ((), 1)


@pytest.mark.parametrize("body", ["reply to fatima@example.com", "Fatima Al Mansoori asked for a refund"])
def test_customer_details_move_a_finding_up_to_customer_data_and_keep_its_trust(body: str) -> None:
    checked = check_findings([draft(body, data_class=DataClass.INTERNAL, trust=Trust.UNTRUSTED)], NAMES)
    (kept,) = checked.kept
    assert (kept.data_class, kept.trust, kept.body) == (DataClass.CUSTOMER_PII, Trust.UNTRUSTED, body)
    assert checked.retagged == 1


def test_a_finding_already_tagged_customer_data_is_kept_as_it_is() -> None:
    tagged = draft("reply to fatima@example.com", data_class=DataClass.CUSTOMER_PII)
    checked = check_findings([tagged], NAMES)
    assert (checked.kept, checked.retagged) == ((tagged,), 0)


def test_a_clean_finding_is_never_moved_down() -> None:
    internal = draft(data_class=DataClass.INTERNAL)
    assert check_findings([internal], NAMES).kept == (internal,)


def test_without_a_name_list_findings_still_get_the_other_nets() -> None:
    checked = check_findings([draft("Fatima Al Mansoori"), draft("mail fatima@example.com")], None)
    assert [d.data_class for d in checked.kept] == [DataClass.PUBLIC, DataClass.CUSTOMER_PII]


def propose(
    payload: dict[str, object], *, names: KnownNames | None = NAMES, target: str = "issue-12"
) -> Action:
    action, _found = checked_action(
        tenant_id=TENANT,
        project_id=EVA,
        run_id=RUN,
        tool="github.comment",
        level=ActionLevel.DRAFT,
        payload=payload,
        target_kind="issue",
        target_ref=target,
        data_classes=(DataClass.INTERNAL,),
        idempotency_key="monitoring:evt-1:github.comment:issue-12",
        names=names,
    )
    return action


def test_a_clean_proposal_passes() -> None:
    action = propose({"body": "The build failed on main: the test step timed out."})
    assert action.leak_check_passed
    assert action.data_classes == (DataClass.INTERNAL,)


@pytest.mark.parametrize(
    ("payload", "target", "customer"),
    [
        ({"body": f"use {TOKEN}"}, "issue-12", False),
        ({"body": "ask fatima@example.com"}, "issue-12", True),
        ({"body": "a reply"}, "fatima@example.com", True),  # the target is checked too
        ({"body": {"nested": ["Fatima Al Mansoori"]}}, "issue-12", True),  # every part of the payload
    ],
)
def test_a_proposal_with_a_secret_or_customer_details_fails_and_can_never_be_approved(
    payload: dict[str, object], target: str, customer: bool
) -> None:
    action = propose(payload, target=target)
    assert not action.leak_check_passed
    assert (DataClass.CUSTOMER_PII in action.data_classes) is customer
    with pytest.raises(ValidationError, match="leak check"):
        Action.model_validate(
            {
                **action.model_dump(),
                "status": ActionStatus.APPROVED,
                "approved_by": TENANT,
                "approved_at": "2026-10-27T09:00:00Z",
            }
        )


def test_a_proposal_with_no_name_list_fails_closed() -> None:
    action, found = checked_action(
        tenant_id=TENANT,
        project_id=EVA,
        run_id=RUN,
        tool="github.comment",
        level=ActionLevel.DRAFT,
        payload={"body": "The build failed."},
        target_kind="issue",
        target_ref="issue-12",
        data_classes=(),
        idempotency_key="k",
        names=None,
    )
    assert (action.leak_check_passed, found.hits) == (False, ("no_name_list",))


def test_a_proposal_declared_as_customer_data_never_passes() -> None:
    action, _ = checked_action(
        tenant_id=TENANT,
        project_id=EVA,
        run_id=RUN,
        tool="email.draft",
        level=ActionLevel.DRAFT,
        payload={"body": "Thanks for writing."},
        target_kind="email",
        target_ref="thread-9",
        data_classes=(DataClass.CUSTOMER_PII,),
        idempotency_key="k",
        names=NAMES,
    )
    assert not action.leak_check_passed  # replies to customers wait for the support lane's own ADR-SEC


def test_only_checked_action_ever_sets_leak_check_passed() -> None:
    """No other code may decide a proposal's leak check (the model and the store only carry the field)."""
    allowed = {SRC / "core/runner/leak.py"}
    carriers = {SRC / "core/sdk/models.py", SRC / "core/store/records.py"}  # the field and its column
    offenders = []
    for path in SRC.rglob("*.py"):
        if path in allowed or path in carriers:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "leak_check_passed":
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
