"""Production publishing boundaries.

This package holds the outbound transport for publishing. It is deliberately
separate from ``tools/wordpress.py``, which is bound to ``BaseTool``,
``LegacyTask``, and process-global ``Config`` semantics that do not fit the
approval-gated publication lifecycle.

Nothing here may import persistence, SQLite, or a repository. A gateway
converts one immutable command into one classified outcome and has no
authority over task, run, or publication state.
"""
