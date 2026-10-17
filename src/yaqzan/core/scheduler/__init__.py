"""Clock and event triggers that wake collectors and agents (debounced, once-only catch-up).

Phase 1+. Must never: run agent logic itself.
"""
