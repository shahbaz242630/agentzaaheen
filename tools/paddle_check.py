"""Read-only check of the Paddle connector against a real Paddle **sandbox** account.

Fetches subscriptions, payments and refunds through the real connector and the
core's one HTTP client, then prints the metrics our code computes, so they can
be compared with the Paddle dashboard by eye. Prints counts and amounts only:
never ids, never the key.

The key is read from the environment variable you name (never printed). This
tool talks to the sandbox only; production reads its key from the vault.

Usage:
    uv run python -m tools.paddle_check --key-env PADDLE_SANDBOX_API_KEY [--days 30]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import SecretStr

from yaqzan.core import metrics
from yaqzan.core.net import JsonClient, NetPolicy
from yaqzan.core.sdk import RevenueBatch, SourceError
from yaqzan.plugins.connectors.paddle import ALLOWED_HOSTS, API_VERSION_HEADER, SANDBOX_BASE_URL, PaddleSource


async def fetch(
    key: SecretStr, now: datetime, transport: httpx.AsyncBaseTransport | None = None
) -> RevenueBatch:
    """One full read-only fetch from the sandbox."""
    async with JsonClient(
        SANDBOX_BASE_URL,
        policy=NetPolicy(allowed_hosts=ALLOWED_HOSTS),
        headers=API_VERSION_HEADER,
        secret_headers={"Authorization": SecretStr(f"Bearer {key.get_secret_value()}")},
        transport=transport,
    ) as http:
        return await PaddleSource(http).fetch(payments_since=None, now=now)


def report(batch: RevenueBatch, now: datetime, days: int) -> list[str]:
    """The lines to print: numbers only."""
    start = now - timedelta(days=days)
    subs, pays, backs = batch.subscriptions, batch.payments, batch.refunds
    window = f"last {days} days"
    return [
        f"paddle sandbox, read at {now.isoformat(timespec='seconds')}",
        f"fetched: {len(subs)} subscriptions, {len(pays)} payment attempts, {len(backs)} refunds/chargebacks",
        f"MRR (minor units per currency): {metrics.mrr(subs)}",
        f"active subscribers: {metrics.active_subscribers(subs)}   trials: {metrics.trials(subs)}",
        f"{window}, failed payments: {metrics.failed_payments(pays, start, now)}",
        f"{window}, refunds: {metrics.refunds(backs, start, now)}",
        f"{window}, cash revenue incl. tax: {metrics.cash_revenue(pays, backs, start, now, tax=True)}",
        f"{window}, cash revenue excl. tax: {metrics.cash_revenue(pays, backs, start, now, tax=False)}",
    ]


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
) -> int:
    """Run the check; return 0 on success, 1 if the key is missing or Paddle refused."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key-env", required=True, help="name of the env var holding a SANDBOX key")
    parser.add_argument("--days", type=int, default=30)
    ns = parser.parse_args(argv)
    raw = (os.environ if environ is None else environ).get(ns.key_env, "")
    if not raw:
        print(f"paddle-check: {ns.key_env} is not set (could not run, not a pass).")
        return 1
    if "_live_" in raw:
        print("paddle-check: that looks like a LIVE key; this tool only talks to the sandbox.")
        return 1
    moment = now or datetime.now(UTC)
    try:
        batch = asyncio.run(fetch(SecretStr(raw), moment, transport))
    except SourceError as exc:
        print(f"paddle-check: failed with {exc.code.value}")
        return 1
    for line in report(batch, moment, ns.days):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
