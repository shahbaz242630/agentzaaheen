"""GitHub connector: read-only code health of one repository (METRICS §6).

What it does:
    ``GitHubSource`` reads one repository's main branch, counts its open
    issues and open pull requests (search totals, never the items), and works
    out how the newest commit on the main branch built across its workflows,
    through the ``JsonGetter`` the core hands it. ``FakeGitHubSource`` serves a
    fixed shape for tests. ``GitHubBuildLog`` reads, only when triage asks,
    the newest failed run's first failed job log (ADR-029): a cleaned,
    redacted excerpt that is always untrusted; ``FakeGitHubBuildLog`` stands
    in for it.

Public interface:
    ``GitHubSource``, ``FakeGitHubSource``, ``GitHubBuildLog``,
    ``FakeGitHubBuildLog``, ``BASE_URL``, ``ALLOWED_HOSTS``, ``LOG_HOST``,
    ``API_HEADERS``, ``SOURCE``. Manifest: ``plugin.yaml`` next to this file.

How to test:
    ``uv run pytest tests/unit/test_github_mapping.py tests/contract/test_repo_source_contract.py``
    ``uv run pytest tests/unit/test_github_logs.py tests/contract/test_build_log_contract.py``
    (a pretend GitHub; no real network).

Must never:
    Call anything but its named GET endpoints; read an issue title, a body, a
    name or a commit message (people write those, so they are untrusted), the
    one build log aside, which is read only on demand and handed on untrusted;
    hold or log the token (the core's client carries it); guess a count
    GitHub says is incomplete.
"""

from yaqzan.plugins.connectors.github.adapter import ALLOWED_HOSTS, API_HEADERS, BASE_URL, GitHubSource
from yaqzan.plugins.connectors.github.fake import FakeGitHubBuildLog, FakeGitHubSource
from yaqzan.plugins.connectors.github.logs import LOG_HOST, GitHubBuildLog
from yaqzan.plugins.connectors.github.mapping import SOURCE

__all__ = [
    "ALLOWED_HOSTS",
    "API_HEADERS",
    "BASE_URL",
    "LOG_HOST",
    "SOURCE",
    "FakeGitHubBuildLog",
    "FakeGitHubSource",
    "GitHubBuildLog",
    "GitHubSource",
]
