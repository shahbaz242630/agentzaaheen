"""A pretend Healthchecks for tests: one fixed answer for the checks list (or an error code to raise)."""

from __future__ import annotations

from collections.abc import Mapping

from agentzaaheen.core.sdk import SourceError, SourceErrorCode


class FakeHealthchecksApi:
    """``answer`` is the JSON the checks list returns, or a ``SourceErrorCode`` to raise. Records each ask."""

    def __init__(self, answer: dict[str, object] | SourceErrorCode) -> None:
        self.answer = answer
        self.asked: list[tuple[str, dict[str, str]]] = []

    async def get_json(self, path: str, params: Mapping[str, str]) -> dict[str, object]:
        """The fixed answer for ``path``."""
        self.asked.append((path, dict(params)))
        if isinstance(self.answer, SourceErrorCode):
            raise SourceError(self.answer, "the fake was told to fail")
        return self.answer
