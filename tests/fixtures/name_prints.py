"""A name printer with a made-up key, for tests (the real key lives in a key file on the machine)."""

from agentzaaheen.core.vault import MasterKey, NamePrinter

PRINTER = NamePrinter(MasterKey("test-names", bytes(range(32))))
