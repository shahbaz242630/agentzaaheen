"""Pretend certificate reads and a pretend registry for tests."""

from __future__ import annotations

from collections.abc import Mapping

from agentzaaheen.core.sdk import CertAnswer, SourceError, SourceErrorCode


class FakeCertGetter:
    """``answers`` maps host to its answer; a host that is missing (or maps to None) does not answer."""

    def __init__(self, answers: Mapping[str, CertAnswer | None]) -> None:
        self.answers = dict(answers)
        self.asked: list[str] = []

    async def certificate_of(self, host: str) -> CertAnswer:
        """The fixed answer for ``host``, or ``SourceError(UNAVAILABLE)``."""
        self.asked.append(host)
        answer = self.answers.get(host)
        if answer is None:
            raise SourceError(SourceErrorCode.UNAVAILABLE, "the fake host does not answer")
        return answer


class FakeRdap:
    """``answers`` maps a path to its JSON (or to an error code to raise). Records each ask."""

    def __init__(self, answers: Mapping[str, dict[str, object] | SourceErrorCode]) -> None:
        self.answers = dict(answers)
        self.asked: list[tuple[str, dict[str, str]]] = []

    async def get_json(self, path: str, params: Mapping[str, str]) -> dict[str, object]:
        """The fixed object for ``path`` (a path not given is refused)."""
        self.asked.append((path, dict(params)))
        answer = self.answers.get(path, SourceErrorCode.REFUSED)
        if isinstance(answer, SourceErrorCode):
            raise SourceError(answer, "the fake was told to fail")
        return answer
