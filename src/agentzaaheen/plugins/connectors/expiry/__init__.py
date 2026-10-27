"""Certificate and domain dates connector: when each certificate and domain ends (F11 slice 5b).

What it does:
    ``CertSource`` reads each host's certificate through the ``CertGetter``
    the core hands it (one TLS handshake: valid or not, and the end date).
    ``DomainSource`` reads each .com/.net domain's end date from the public
    registry (RDAP) through a ``JsonGetter``. Both give an ``ExpiryReport``.
    ``FakeCertGetter`` and ``FakeRdap`` stand in for them in tests.

Public interface:
    ``CertSource``, ``DomainSource``, ``FakeCertGetter``, ``FakeRdap``,
    ``RDAP_URL``, ``ALLOWED_HOSTS``, ``API_HEADERS``, ``RDAP_TLDS``,
    ``SOURCE``. Manifest: ``plugin.yaml``.

How to test:
    ``uv run pytest tests/unit/test_expiry.py`` (no real network).

Must never:
    Ask a site for a page, or send the registry anything but our own domain
    names; hold a key (none is needed).
"""

from agentzaaheen.plugins.connectors.expiry.adapter import (
    ALLOWED_HOSTS,
    API_HEADERS,
    RDAP_TLDS,
    RDAP_URL,
    SOURCE,
    CertSource,
    DomainSource,
)
from agentzaaheen.plugins.connectors.expiry.fake import FakeCertGetter, FakeRdap

__all__ = [
    "ALLOWED_HOSTS",
    "API_HEADERS",
    "RDAP_TLDS",
    "RDAP_URL",
    "SOURCE",
    "CertSource",
    "DomainSource",
    "FakeCertGetter",
    "FakeRdap",
]
