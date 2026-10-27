"""Named read queries: the one POST the client sends, and only the SELECTs it was made with (ADR-032).

No real network: every request goes to an ``httpx.MockTransport`` handler.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence

import httpx
import pytest
from pydantic import SecretStr

from agentzaaheen.core.net import JsonClient, NetError, NetErrorCode, NetPolicy, check_read_query
from agentzaaheen.core.sdk import SourceError

HOST = "api.example.com"
POLICY = NetPolicy(allowed_hosts=frozenset({HOST}), max_attempts=3)
SQL = "SELECT a, b FROM t WHERE c >= ?1"
FAKE_KEY = "cf_" + "k" * 30  # built here, never a real key

Handler = Callable[[httpx.Request], httpx.Response]


async def _no_sleep(_: float) -> None:
    return None


def query(
    handler: Handler,
    sql: str = SQL,
    params: Sequence[int | str] = (5,),
    queries: frozenset[str] = frozenset({SQL}),
) -> dict[str, object]:
    async def body() -> dict[str, object]:
        async with JsonClient(
            f"https://{HOST}",
            policy=POLICY,
            secret_headers={"Authorization": SecretStr(f"Bearer {FAKE_KEY}")},
            transport=httpx.MockTransport(handler),
            sleep=_no_sleep,
            jitter=lambda: 0.0,
            read_queries=queries,
        ) as c:
            return await c.query_json("/db/query", sql, params)

    return asyncio.run(body())


def test_a_named_query_is_posted_as_json_with_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True})

    assert query(handler, params=(5, "eva")) == {"success": True}
    request = seen[0]
    assert (request.method, request.url.host, request.url.path) == ("POST", HOST, "/db/query")
    assert json.loads(request.content) == {"sql": SQL, "params": [5, "eva"]}
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"


def test_a_read_query_is_retried_like_a_get() -> None:
    answers = iter([httpx.Response(503), httpx.Response(200, json={"ok": 1})])
    assert query(lambda _: next(answers)) == {"ok": 1}


def never(_: httpx.Request) -> httpx.Response:
    raise AssertionError("nothing may be sent")


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("SELECT a, b FROM t WHERE c >= ?2", (5,)),  # not one of the client's queries
        ("DELETE FROM t", ()),
        (SQL, (True,)),  # a flag is not a plain value
        (SQL, ("x" * 65,)),
        (SQL, ("line\nbreak",)),
        (SQL, tuple(range(9))),  # too many
        (SQL, (1.5,)),  # only whole numbers and words
    ],
)
def test_anything_else_is_refused_before_a_byte_leaves(sql: str, params: Sequence[int | str]) -> None:
    with pytest.raises(NetError) as caught:
        query(never, sql=sql, params=params)
    assert caught.value.net_code is NetErrorCode.BLOCKED
    assert isinstance(caught.value, SourceError)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t",
        "select a from t",  # one spelling only: the plain upper-case SELECT
        "SELECT a FROM t; DROP TABLE t",
        "SELECT a FROM t -- note",
        "SELECT a FROM t /* note */",
        "SELECT a FROM t\nWHERE b = 1",
        "SELECT " + "a" * 1100,
    ],
)
def test_a_client_cannot_be_made_with_a_query_that_is_not_one_plain_select(sql: str) -> None:
    with pytest.raises(ValueError, match="one plain SELECT"):
        check_read_query(sql)
    with pytest.raises(ValueError, match="one plain SELECT"):
        JsonClient(f"https://{HOST}", policy=POLICY, read_queries=frozenset({sql}))


def test_a_refused_answer_is_an_error_with_the_post_named() -> None:
    with pytest.raises(NetError) as caught:
        query(lambda _: httpx.Response(403))
    assert caught.value.net_code is NetErrorCode.AUTH
    assert (caught.value.operation, caught.value.attempt) == ("POST /db/query", 1)
