"""The broker: the only code that opens a stored credential (SECURITY §5.1).

What it does:
    Joins the vault (encryption) to the store (rows): saves a key sealed,
    with its expiry and access level beside it, and opens it again for one
    use. Later it also hands keys to connector processes over a private
    channel; for now callers in the core use :func:`open_credential` directly.

Public interface:
    ``save_credential``, ``open_credential``, ``SealedSlot`` (one slot, for callers
    that may not see the vault), ``BrokerError``, ``BrokerErrorCode``.

How to test:
    ``uv run pytest tests/integration/test_broker.py`` (needs the dev database).

Must never:
    Log, print or return a secret except as a ``SecretStr`` to its caller;
    be imported by a plugin, the SDK, the store, metrics or the network module.
"""

from agentzaaheen.core.broker.keys import (
    BrokerError,
    BrokerErrorCode,
    SealedSlot,
    open_credential,
    save_credential,
)

__all__ = ["BrokerError", "BrokerErrorCode", "SealedSlot", "open_credential", "save_credential"]
