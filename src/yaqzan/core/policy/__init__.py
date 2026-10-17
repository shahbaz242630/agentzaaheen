"""The policy gate: the only door agents have to tools.

Checks allowlists, argument schemas, permission levels, ownership and budgets;
marks every tool result untrusted; writes the audit log. Phase 2+.
Must never: let an LLM authorise anything.
"""
