"""DocDedupe — local content-level near-duplicate finder for documents.

The package is intentionally small at M0: only the CLI shell, the single source of
truth for numeric settings, and the SQLite schema are implemented. Every module that
is not implemented yet fails explicitly with exit code 3 instead of returning an
empty success result (see ``plan/05-里程碑与发布.md``).
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
