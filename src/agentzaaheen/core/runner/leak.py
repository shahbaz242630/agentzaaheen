"""Before anything leaves a run: the leak check (ADR-036 checkpoint 4, F11 slice 3).

Every finding a run hands back and every proposal is checked in code, so the
rule holds even when the model forgets it. The check names only the kind of
thing found (``secret``, ``email``, ``known_customer_name``), never the text.

- **A secret** (a key, token, password in a web address, private key) is never
  kept: a finding holding one is held back (not saved; the run row and the audit
  chain say so), and a proposal holding one fails the check.
- **Customer details** (an email, phone, card or payment customer ID, or one of
  our own customers' names) in a finding that did not say so: the finding is
  moved up to customer data, so it can never reach a cloud model (ADR-023). The
  data class only goes up, never down, and the trust tag is kept as given: an
  untrusted finding stays untrusted. Without a name list, names are not checked
  for findings (they stay inside Agent Zaaheen); the other nets still run.
- **A proposal** (something that would leave: an email, a comment) passes only
  with no secret, no customer details and a name list to check against (fails
  closed, as the Claude door does). One that fails is still saved for the
  Chairman to see, but can never be approved: the model and the database both
  refuse. :func:`checked_action` is the only code that sets
  ``leak_check_passed``. Replies to a customer (which must hold that customer's
  details) wait for the support lane and its own ADR-SEC: customer-scoped, so
  only *another* customer's data fails.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from agentzaaheen.core.llm_gateway import Found, KnownNames, customer_details_in
from agentzaaheen.core.llm_gateway.cloud_check import MAX_CHECKED_CHARS
from agentzaaheen.core.runner.agent import FindingDraft
from agentzaaheen.core.sdk import Action, ActionLevel, DataClass, contains_secret, max_data_class

SECRET = "secret"  # noqa: S105  # nosec B105  # the kind of thing found, not a password
TOO_LONG = Found.TOO_LONG.value
NO_NAME_LIST = Found.NO_NAME_LIST.value


@dataclass(frozen=True)
class LeakCheck:
    """What the check found: kinds only, sorted, never the text. No hits means it passed."""

    hits: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """True when nothing was found."""
        return not self.hits

    @property
    def secret(self) -> bool:
        """True when a key, token, password or private key was found."""
        return SECRET in self.hits

    @property
    def customer(self) -> bool:
        """True when customer details or one of our customers' names were found."""
        return any(hit not in {SECRET, TOO_LONG, NO_NAME_LIST} for hit in self.hits)


def leak_check(texts: Sequence[str], names: KnownNames | None) -> LeakCheck:
    """Secrets, our customers' names (when there is a list) and customer details in ``texts``."""
    if sum(len(text) for text in texts) > MAX_CHECKED_CHARS:
        return LeakCheck((TOO_LONG,))  # too long to check is never "nothing found"
    hits: set[str] = set()
    for text in texts:
        if contains_secret(text):
            hits.add(SECRET)
        named = names.match_in(text) if names is not None else None
        if named is not None:
            hits.add(named.value)
        details = customer_details_in(text)
        if details is not None:
            hits.add(details.value)
    return LeakCheck(tuple(sorted(hits)))


@dataclass(frozen=True)
class CheckedFindings:
    """A run's findings after the check: the ones kept (some moved up to customer data) and how many not."""

    kept: tuple[FindingDraft, ...]
    held_back: int
    retagged: int


def check_findings(drafts: Iterable[FindingDraft], names: KnownNames | None) -> CheckedFindings:
    """Hold back each finding with a secret; move up to customer data each one holding customer details."""
    kept: list[FindingDraft] = []
    held_back = retagged = 0
    for draft in drafts:
        found = leak_check(_texts_of(draft), names)
        if found.secret or TOO_LONG in found.hits:
            held_back += 1
            continue
        if found.customer and draft.data_class is not DataClass.CUSTOMER_PII:
            raised = max_data_class(draft.data_class, DataClass.CUSTOMER_PII)
            kept.append(draft.model_copy(update={"data_class": raised}))
            retagged += 1
            continue
        kept.append(draft)
    return CheckedFindings(tuple(kept), held_back, retagged)


def _texts_of(draft: FindingDraft) -> list[str]:
    """Every piece of text in a finding: title, body, its metric links and its evidence addresses."""
    texts = [draft.title, draft.body]
    texts += [f"{key} {value}" for key, value in draft.metric_refs.items()]
    texts += [e.url for e in draft.evidence if e.url is not None]
    texts += [e.metric_id for e in draft.evidence if e.metric_id is not None]
    return texts


def checked_action(  # noqa: PLR0913  # each is a separate field of the proposal
    *,
    tenant_id: UUID,
    project_id: UUID,
    run_id: UUID,
    tool: str,
    level: ActionLevel,
    payload: Mapping[str, object],
    target_kind: str,
    target_ref: str,
    data_classes: Sequence[DataClass],
    idempotency_key: str,
    names: KnownNames | None,
) -> tuple[Action, LeakCheck]:
    """A proposal with its leak check done here: the only way ``leak_check_passed`` is ever set."""
    shown = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)  # exactly what would leave
    found = leak_check([shown, target_ref, target_kind], names)
    if names is None:
        found = LeakCheck(tuple(sorted({*found.hits, NO_NAME_LIST})))
    classes = set(data_classes)
    if found.customer:
        classes.add(DataClass.CUSTOMER_PII)
    passed = found.passed and DataClass.CUSTOMER_PII not in classes
    action = Action(
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        tool=tool,
        level=level,
        payload=dict(payload),
        target_kind=target_kind,
        target_ref=target_ref,
        data_classes=tuple(sorted(classes, key=lambda c: c.rank)),
        leak_check_passed=passed,
        idempotency_key=idempotency_key,
    )
    return action, found
