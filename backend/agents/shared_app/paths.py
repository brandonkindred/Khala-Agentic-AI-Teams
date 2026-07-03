"""Idempotent ``sys.path`` bootstrap shared by team ``api`` packages.

Every team's ``api`` package prepends a couple of directories to ``sys.path`` so
that bare, team-local imports resolve when the app is launched by uvicorn from an
arbitrary working directory. :func:`bootstrap_syspath` centralizes that
otherwise-copy-pasted idempotent insert.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Union


def bootstrap_syspath(*dirs: Union[str, Path], must_exist: bool = False) -> None:
    """Prepend each of ``dirs`` to ``sys.path`` (front), once, in order.

    Preconditions:
        - Each entry in ``dirs`` is a ``str`` or ``Path`` naming a directory.
    Postconditions:
        - After the call, each supplied dir (that exists, when ``must_exist`` is
          True) is present in ``sys.path`` exactly once; a dir already on the
          path is left where it is, and — with ``must_exist`` — a missing dir is
          skipped. Later args end up earlier in ``sys.path`` (each is inserted at
          index 0), matching the hand-rolled two-insert idiom this replaces.
    Invariants:
        - Idempotent: calling again with the same dirs does not duplicate entries.
    """
    for d in dirs:
        p = Path(d)
        if must_exist and not p.exists():
            continue
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)
