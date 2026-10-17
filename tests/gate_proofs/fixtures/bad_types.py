"""Gate proof fixture: MUST fail mypy --strict. Never fix this file."""


def add(a: int, b: int) -> int:
    return a + b


def untyped(x):
    return x


total: int = add("1", 2)
