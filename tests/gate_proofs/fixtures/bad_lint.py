"""Gate proof fixture: MUST fail ruff. Never fix this file."""
import os, sys
def f(x):
    print(x)
    try:
        return eval(x)
    except:
        pass
