"""A pretend set of addresses for tests: each url answers a fixed status, or does not answer at all."""

from __future__ import annotations

from collections.abc import Mapping

from agentzaaheen.core.sdk import SourceError, SourceErrorCode, StatusAnswer


class FakeStatusGetter:
    """``answers`` maps url → status; a url that is missing (or maps to None) does not answer."""

    def __init__(self, answers: Mapping[str, int | None], elapsed_ms: int = 120) -> None:
        self.answers = dict(answers)
        self.elapsed_ms = elapsed_ms
        self.asked: list[str] = []

    async def status_of(self, url: str) -> StatusAnswer:
        """The fixed status for ``url``, or ``SourceError(UNAVAILABLE)``."""
        self.asked.append(url)
        status = self.answers.get(url)
        if status is None:
            raise SourceError(SourceErrorCode.UNAVAILABLE, "the fake address does not answer")
        return StatusAnswer(status=status, elapsed_ms=self.elapsed_ms)
