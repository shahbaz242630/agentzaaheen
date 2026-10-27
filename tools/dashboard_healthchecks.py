"""Healthchecks on this laptop: which checks belong to which project, and the newest read (F11 slice 5a).

The list lives in ``projects/healthchecks.yaml`` (git-ignored and path-guarded,
Rulebook §7b). Each project names the slugs of its checks::

    zaaheen: [zaaheen, zaaheen-workers]
    sandoq-kin: [sandoq-kin]

Checks that belong to no project (the reminder checks, Agent Zaaheen's own
heartbeats) are not listed: they alert the phone themselves. The read-only
key comes from an environment variable (``--healthchecks-key-env``) and is
never printed. :class:`HealthchecksReader` reads every check with one GET,
only in a Monitoring round that has an agent to wake, and keeps the newest
read; a failed read keeps the last one, which turns "not read" by its age.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from pydantic import SecretStr

from agentzaaheen.core.net import JsonClient, NetPolicy
from agentzaaheen.core.sdk import CHECK_SLUG, HeartbeatReport, SourceError, get_logger, say
from agentzaaheen.core.sdk.heartbeats import MAX_SLUG
from agentzaaheen.plugins.connectors import healthchecks
from tools.dashboard_sources import ListNotUsableError

MAX_LIST_BYTES = 16 * 1024
MAX_SLUGS = 20  # per project

Clock = Callable[[], datetime]
HealthchecksList = dict[str, tuple[str, ...]]


class HealthchecksListError(Exception):
    """The list could not be used; the message says why in plain words."""


def load_healthchecks(path: Path, projects: Collection[str]) -> HealthchecksList:
    """Each project's check slugs from ``path``; only known projects are allowed."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise HealthchecksListError(f"the Healthchecks list {path} could not be read") from None
    if len(raw) > MAX_LIST_BYTES:
        raise HealthchecksListError("the Healthchecks list is too big")
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise HealthchecksListError("the Healthchecks list is not valid YAML") from None
    if not isinstance(data, dict) or not data:
        raise HealthchecksListError("the Healthchecks list must map each project to its check slugs")
    out: HealthchecksList = {}
    for project, slugs in data.items():
        if project not in projects:
            raise HealthchecksListError(f"the Healthchecks list names an unknown project: {project!r}")
        if not isinstance(slugs, list) or not slugs or len(slugs) > MAX_SLUGS:
            raise HealthchecksListError(f"{project}: give a list of 1 to {MAX_SLUGS} check slugs")
        for slug in slugs:
            if not isinstance(slug, str) or len(slug) > MAX_SLUG or not re.fullmatch(CHECK_SLUG, slug):
                raise HealthchecksListError(f"{project}: {slug!r} is not a check slug")
        out[project] = tuple(dict.fromkeys(slugs))
    return out


def healthchecks_list(path: Path | None, projects: Collection[str]) -> HealthchecksList:
    """The local list given with ``--healthchecks`` (empty without one); a broken list is said and raised."""
    if path is None:
        return {}
    log = get_logger("agentzaaheen.settings")
    try:
        found = load_healthchecks(path, projects)
    except HealthchecksListError as exc:
        print(f"dashboard: {exc}.")  # the exact rule, on the console only: it can quote the file
        say(log, "settings_refused", setting="Healthchecks")
        raise ListNotUsableError from None
    say(log, "settings_loaded", setting="Healthchecks", entries=len(found))
    return found


class HealthchecksReader:
    """Reads every check with the read-only key; :attr:`latest` is the newest good read (None before)."""

    def __init__(
        self, key: SecretStr, *, clock: Clock, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._key = key
        self._clock = clock
        self._transport = transport
        self._log = get_logger("agentzaaheen.healthchecks")
        self.latest: HeartbeatReport | None = None

    async def refresh(self) -> None:
        """Read once; a failure is logged and the last read kept (it turns "not read" by its age)."""
        try:
            async with JsonClient(
                healthchecks.BASE_URL,
                policy=NetPolicy(allowed_hosts=healthchecks.ALLOWED_HOSTS),
                headers=healthchecks.API_HEADERS,
                secret_headers={healthchecks.KEY_HEADER: self._key},
                transport=self._transport,
            ) as http:
                report = await healthchecks.HealthchecksSource(http).fetch(now=self._clock())
        except SourceError as exc:
            say(self._log, "healthchecks_read_failed", code=exc.code.value)
            return
        self.latest = report
        say(self._log, "healthchecks_read_done", checks=len(report.checks))


def healthchecks_reader(
    env: Mapping[str, str],
    key_env: str | None,
    *,
    clock: Clock,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HealthchecksReader | None:
    """The reader, if ``key_env`` holds a usable key (said on the console, never shown)."""
    if not key_env:
        return None
    raw = env.get(key_env, "").strip()
    if not raw or not raw.isascii() or not raw.isprintable() or " " in raw:
        print(f"dashboard: {key_env} is not set (or not usable), so Monitoring cannot read Healthchecks.")
        return None
    print("dashboard: Monitoring reads Healthchecks (read-only key) in each round.")
    return HealthchecksReader(SecretStr(raw), clock=clock, transport=transport)
