"""A pretend Cloudflare D1 for tests: one fixed answer for the query (or an error code to raise)."""

from __future__ import annotations

from collections.abc import Sequence

from agentzaaheen.core.sdk import SourceError, SourceErrorCode


def d1_answer(rows: list[dict[str, object]]) -> dict[str, object]:
    """What Cloudflare answers for one successful statement with ``rows``."""
    return {"success": True, "errors": [], "result": [{"success": True, "results": rows, "meta": {}}]}


class FakeD1:
    """``answer`` is the JSON the query returns, or a ``SourceErrorCode`` to raise. Records each ask."""

    def __init__(self, answer: dict[str, object] | SourceErrorCode) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str, list[int | str]]] = []

    async def query_json(self, path: str, sql: str, params: Sequence[int | str]) -> dict[str, object]:
        """The fixed answer for ``sql``."""
        self.asked.append((path, sql, list(params)))
        if isinstance(self.answer, SourceErrorCode):
            raise SourceError(self.answer, "the fake was told to fail")
        return self.answer
