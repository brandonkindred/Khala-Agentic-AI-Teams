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

# Upper bound on a validated identifier. It is derived from the per-name limit
# on the common Linux filesystems (ext4, XFS), minus the decoration the stores
# add when they turn the identifier into an actual filename, so the *generated*
# name — not just the identifier — always fits. The worst case is
# ``ProvisionerStateStore._save``'s tempfile, ``.{name}.XXXXXXXX.json`` (two
# dots + an 8-char random token + a 5-char suffix ≈ 15 bytes); the env/credential
# stores add far less (``.json`` / ``.enc``). Reserving 20 bytes covers that with
# a small margin. This is a backstop against pathologically long inputs
# (filesystem-limit errors, wasted memory, resource-exhaustion DoS), not a
# traversal guard — and real identifiers are far shorter (agent_ids are <=120).
_NAME_MAX = 255
_MAX_SUFFIX_OVERHEAD = 20
_MAX_COMPONENT_LEN = _NAME_MAX - _MAX_SUFFIX_OVERHEAD  # 235


def safe_path_component(value: str, *, kind: str = "identifier") -> str:
    """Validate that ``value`` is safe to embed in a filename and return it.

    A value is safe when it is a ``str`` of at most ``_MAX_COMPONENT_LEN``
    characters that matches ``[A-Za-z0-9._-]+`` and is not the bare directory
    token ``.`` or ``..``. That set covers every real identifier — slugs, UUIDs,
    and dotted names such as ``blog.writer`` — while excluding anything that
    could change directories: path separators (``/``, ``\\``), NUL bytes, and
    whitespace are already outside the character class. A double dot *inside* a
    longer name (e.g. ``a..b``) is harmless, because the class forbids
    separators, so ``..`` can only traverse when it is the whole component — and
    such names are therefore accepted.

    Preconditions:
        * ``value`` is a ``str``. ``kind`` only customises the error message
          (e.g. ``"agent_id"``).
    Postconditions:
        * Returns ``value`` byte-for-byte when it is safe, so callers that key
          files by the identifier round-trip unchanged.
        * Raises ``ValueError`` (never silently coerces) when ``value`` is not a
          string, exceeds ``_MAX_COMPONENT_LEN``, or is not a safe component.
    """
    if not isinstance(value, str):
        raise ValueError(f"unsafe {kind} for a filesystem path: {value!r}")
    # Length first (cheap) so a pathological megabyte input is rejected before
    # the regex scans it.
    if len(value) > _MAX_COMPONENT_LEN:
        raise ValueError(f"{kind} exceeds maximum length of {_MAX_COMPONENT_LEN}: {len(value)}")
    if _SAFE_COMPONENT.fullmatch(value) is None or value in (".", ".."):
        raise ValueError(f"unsafe {kind} for a filesystem path: {value!r}")
    return value


def candidate_paths(primary: Path, legacy_dirs: Iterable[Path]) -> list[Path]:
    """Return the ``[primary, *legacy]`` lookup paths for a store record.

    Stores keyed by an identifier look a record up in the primary store first,
    then in the pre-``AGENT_CACHE`` legacy directories. The environment and
    credential stores share this shape and differ only in their legacy directory
    list, so this helper removes the duplicated construction.

    ``primary`` is the caller's already-validated primary path (built through the
    store's guarded ``_*_file`` method, which runs :func:`safe_path_component`).
    Each legacy candidate reuses ``primary.name`` — the same validated
    ``{identifier}{ext}`` filename — in a legacy directory, so the identifier is
    validated exactly once, at the caller, and no candidate is built from an
    unchecked identifier.

    Preconditions:
        * ``primary`` was produced by a guarded ``_*_file`` method (its filename
          component is already validated).
    Postconditions:
        * Returns ``primary`` followed by ``legacy / primary.name`` for each
          legacy directory, in order.
    """
    return [primary, *(legacy / primary.name for legacy in legacy_dirs)]
