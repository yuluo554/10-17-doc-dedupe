"""Local review page (module 4).

The package deliberately has no imports of its own: everything that needs the
optional web stack (:mod:`docdedupe.web.server`) imports it inside the function
that needs it. That keeps two promises testable:

* ``doc-dedupe --help`` and every non-web command stay runnable on an install
  without the ``web`` extra;
* the missing-extra case surfaces as exit code 3 with an install hint, not as an
  ``ImportError`` traceback — the CLI catches :class:`MissingWebDependency`.
"""

from __future__ import annotations


class MissingWebDependency(RuntimeError):
    """The optional ``web`` extra is not installed.

    Raised instead of letting ``ImportError`` escape so the CLI can translate it
    into the documented exit code (3) with an actionable message, and so a test
    can assert that contract by making ``import fastapi`` fail.
    """

    def __init__(self, package: str = "fastapi") -> None:
        super().__init__(
            "missing dependency %r required for the review page; "
            'install it with: pip install -e ".[web]"' % package
        )
        self.package = package


__all__ = ["MissingWebDependency"]
