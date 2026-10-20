"""A pretend Supabase for tests: serves fixed answers for one project's four endpoints, like the real API."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode

from yaqzan.core.sdk import SourceError, SourceErrorCode


class FakeSupabaseApi:
    """``answers`` maps a path to its JSON (or to an error code to raise). Records what was asked.

    A key may also carry the query (``"/path?services=realtime"``) to answer that exact question differently.
    """

    def __init__(self, answers: Mapping[str, object]) -> None:
        self.answers = dict(answers)
        self.asked: list[tuple[str, dict[str, str]]] = []

    def _answer(self, path: str, params: Mapping[str, str]) -> object:
        self.asked.append((path, dict(params)))
        exact = f"{path}?{urlencode(sorted(params.items()))}" if params else path
        answer = self.answers.get(exact, self.answers.get(path, SourceErrorCode.REFUSED))
        if isinstance(answer, SourceErrorCode):
            raise SourceError(answer, "the fake was told to fail")
        return answer

    async def get_json(self, path: str, params: Mapping[str, str]) -> dict[str, object]:
        """The fixed object for ``path``."""
        answer = self._answer(path, params)
        if not isinstance(answer, dict):
            raise SourceError(SourceErrorCode.BAD_RESPONSE, "not an object")
        return answer

    async def get_json_list(self, path: str, params: Mapping[str, str]) -> list[object]:
        """The fixed array for ``path``."""
        answer = self._answer(path, params)
        if not isinstance(answer, list):
            raise SourceError(SourceErrorCode.BAD_RESPONSE, "not an array")
        return answer
