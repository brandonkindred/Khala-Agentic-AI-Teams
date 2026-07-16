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

# Filenames are built as ``f"{value}.<ext>"``. Permit only the characters real
# identifiers use — alphanumerics plus dot, hyphen, underscore — and nothing
# that can change directories or escape the store (``/``, ``\``, NUL,
# whitespace). ``..`` matches this class yet still traverses, so it is rejected
# explicitly by ``safe_path_component``.
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")


def safe_path_component(value: str, *, kind: str = "identifier") -> str:
    """Return ``value`` unchanged iff it is safe to embed in a filename.

    Preconditions:
        * ``value`` is a ``str`` matching ``[A-Za-z0-9._-]+`` and containing no
          ``..`` segment. Empty strings, path separators (``/`` or ``\\``), NUL
          bytes, whitespace, and ``.``/``..`` traversal tokens all violate the
          precondition.
    Postconditions:
        * Returns ``value`` byte-for-byte when the precondition holds, so callers
          that key files by the identifier round-trip unchanged.
        * Raises ``ValueError`` (never silently coerces) otherwise — the caller
          supplied an identifier that would escape the storage directory.

    ``kind`` only customises the error message (e.g. ``"agent_id"``).
    """
    if (
        not isinstance(value, str)
        or _SAFE_COMPONENT.fullmatch(value) is None
        or ".." in value
        or value == "."
    ):
        raise ValueError(f"unsafe {kind} for a filesystem path: {value!r}")
    return value
