"""API module for the software engineering team.

Importing this package runs the ``_paths`` sys.path bootstrap first so every
api sub-module can rely on the team root being importable.
"""

from . import _paths  # noqa: F401  (side effect: sys.path bootstrap, must run first)
