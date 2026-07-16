"""Filesystem-path-safety guards for identifiers embedded in filenames.

Several stores in this package build a per-record path as
``storage_dir / f"{identifier}.<ext>"`` (environment registry, credential
store, provisioner idempotency state). When the identifier is attacker
controlled — e.g. an ``agent_id`` arriving from the HTTP API — an input like
``"../../etc/passwd"`` escapes ``storage_dir`` and turns the path build into a
path-traversal read/write primitive.

This module centralises the single allowlist every such store shares so the
rule cannot drift between call sites.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

# A safe component matches this class AND is not the directory token "." or
# "..". The class already forbids separators ("/", "\"), NUL bytes, and
# whitespace, so a "``..``" can only change directories when it is the *entire*
# component (rejected in ``safe_path_component`` below); a double dot inside a
# longer name such as "a..b" is not a traversal and is allowed.
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")


def safe_path_component(value: str, *, kind: str = "identifier") -> str:
    """Validate that ``value`` is safe to embed in a filename and return it.

    A value is safe when it matches ``[A-Za-z0-9._-]+`` and is not the bare
    directory token ``.`` or ``..``. That set covers every real identifier —
    slugs, UUIDs, and dotted names such as ``blog.writer`` — while excluding
    anything that could change directories: path separators (``/``, ``\\``),
    NUL bytes, and whitespace are already outside the character class. A double
    dot *inside* a longer name (e.g. ``a..b``) is harmless, because the class
    forbids separators, so ``..`` can only traverse when it is the whole
    component — and such names are therefore accepted.

    Returns ``value`` byte-for-byte when it is safe, so callers that key files
    by the identifier round-trip unchanged. Raises ``ValueError`` (never
    silently coerces) otherwise. ``kind`` only customises the error message
    (e.g. ``"agent_id"``).
    """
    if (
        not isinstance(value, str)
        or _SAFE_COMPONENT.fullmatch(value) is None
        or value in (".", "..")
    ):
        raise ValueError(f"unsafe {kind} for a filesystem path: {value!r}")
    return value


def candidate_paths(
    identifier: str,
    primary_dir: Path,
    legacy_dirs: Iterable[Path],
    extension: str,
    *,
    kind: str = "identifier",
) -> list[Path]:
    """Build the ``[primary, *legacy]`` record paths for a store, guarding once.

    Stores keyed by an identifier look a record up in the primary store first,
    then in the pre-``AGENT_CACHE`` legacy directories. Both the environment and
    credential stores share this shape, differing only in ``extension``
    (``.json`` vs ``.enc``) and their legacy directory list.

    ``identifier`` is validated with :func:`safe_path_component` exactly once and
    the validated value is reused for every candidate, so **no** path — primary
    or legacy — is ever built from an unchecked identifier, independent of
    evaluation order. Raises ``ValueError`` on an unsafe identifier before any
    path is constructed. ``extension`` is appended verbatim (e.g. ``".json"``).
    """
    filename = f"{safe_path_component(identifier, kind=kind)}{extension}"
    return [primary_dir / filename, *(legacy / filename for legacy in legacy_dirs)]
