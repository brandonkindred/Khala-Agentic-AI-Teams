"""Shared utilities for reading repository code and environment helpers.

Consolidates ``_read_repo_code``, ``_truncate_for_context``, and ``_int_env``
that were previously duplicated across backend_agent, orchestrator,
and documentation_agent modules.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Directories excluded from repo scans (build artifacts, VCS, dependency caches)
REPO_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        ".angular",
    }
)

# Extra interpreter/venv caches excluded by repo-inspection views on top of
# REPO_EXCLUDE_DIRS, plus the combined set those views use. Single-sourced here so
# the active repo-inspection tools (agent_repo_tools) and the passive context
# scanner (coding_team._read_repo_context) cannot drift.
REPO_INSPECT_EXTRA_EXCLUDE_DIRS: frozenset[str] = frozenset({"__pycache__", "venv", ".venv"})
REPO_INSPECT_EXCLUDE_DIRS: frozenset[str] = REPO_EXCLUDE_DIRS | REPO_INSPECT_EXTRA_EXCLUDE_DIRS

# Default extensions per agent domain
BACKEND_EXTENSIONS: List[str] = [".py", ".java"]
FRONTEND_EXTENSIONS: List[str] = [".ts", ".tsx", ".html", ".scss"]
FULL_STACK_EXTENSIONS: List[str] = [
    ".py",
    ".ts",
    ".tsx",
    ".java",
    ".yml",
    ".yaml",
]
DOCUMENTATION_EXTENSIONS: List[str] = [
    ".py",
    ".ts",
    ".tsx",
    ".java",
    ".yml",
    ".yaml",
    ".html",
    ".scss",
]


def read_repo_code(
    repo_path: Path,
    extensions: Optional[List[str]] = None,
    *,
    exclude_dirs: Optional[frozenset[str]] = None,
) -> str:
    """Read source files from *repo_path*, concatenated with path headers.

    Parameters
    ----------
    repo_path:
        Root of the repository to scan.
    extensions:
        File suffixes to include (e.g. ``[".py", ".java"]``).
        Defaults to :data:`FULL_STACK_EXTENSIONS`.
    exclude_dirs:
        Directory names to skip.  Defaults to :data:`REPO_EXCLUDE_DIRS`.
        ``.git`` is *always* excluded regardless of this parameter.
    """
    if extensions is None:
        extensions = FULL_STACK_EXTENSIONS
    if exclude_dirs is None:
        exclude_dirs = REPO_EXCLUDE_DIRS

    always_exclude = exclude_dirs | {".git"}

    parts: List[str] = []
    for f in repo_path.rglob("*"):
        if always_exclude & set(f.parts):
            continue
        if f.is_file() and f.suffix in extensions:
            try:
                parts.append(
                    f"### {f.relative_to(repo_path)} ###\n"
                    f"{f.read_text(encoding='utf-8', errors='replace')}"
                )
            except (OSError, UnicodeDecodeError):
                pass
    return "\n\n".join(parts) if parts else "# No code files found"


def read_repo_code_budgeted(
    repo_path: Path,
    *,
    extensions: Iterable[str],
    exclude_dirs: Iterable[str],
    max_chars: int,
    empty: str = "# No code files found",
) -> str:
    """Read source files under *repo_path* until a character budget is hit.

    The single implementation behind the per-domain ``_read_repo_code`` readers in
    the code-v2 and ai-agent-development teams: a streamed ``os.walk`` that prunes
    excluded directories in place (so a checkout with a huge ``node_modules`` /
    ``.git`` / ``.venv`` is never descended into) and emits
    ``--- <relpath> ---\\n<content>\\n`` chunks joined by newlines, stopping once
    adding the next chunk would exceed *max_chars* (whole files only — never a
    partial file).

    Preconditions:
        - ``repo_path`` is an existing directory ``Path``.
        - ``extensions`` are suffixes including the leading dot (e.g. ``.py``).
        - ``max_chars`` is a positive int.
    Postconditions:
        - Returns the concatenated briefing, or *empty* when no eligible file is
          found. The briefing is best-effort by contract and this function never
          raises past precondition checks: a file that cannot be read is skipped
          (logged at WARNING), and a mid-walk filesystem error (e.g. a directory
          deleted by a parallel build while scanning, or an untraversable dir)
          degrades to a briefing built from the entries enumerated *before* the
          error, instead of failing the caller's workflow. Eligible files are
          collected, then sorted, then read — so a walk that aborts partway still
          yields real content, not the empty string.
    """
    assert max_chars > 0, "max_chars must be positive"
    ext_set = frozenset(extensions)
    excl_set = frozenset(exclude_dirs)
    # Stream the walk with ``os.walk`` and prune excluded dirs in place so the
    # traversal never descends into node_modules/.git/etc. The previous
    # ``rglob("*")`` materialized the *entire* tree (including every entry under
    # those excluded dirs) into a list before the sort + budget loop, which is
    # precisely the stat storm a large frontend repo makes expensive. Collecting
    # eligible files only, then sorting, matches the prior output (same files,
    # same order, same budget cut) without enumerating the excluded subtrees.
    # ``OSError`` is the only family a filesystem walk raises; a broader catch
    # would bury real programming errors as empty briefings.
    eligible: List[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(repo_path):
            dirnames[:] = [d for d in dirnames if d not in excl_set]
            for name in filenames:
                f = Path(dirpath) / name
                # is_file() (not just the suffix) guards against special files:
                # a FIFO / socket / device named e.g. ``pipe.py`` would pass the
                # suffix check and block read_text() forever (a hang the
                # try/except in the read loop cannot catch). is_file() is False
                # for those and for broken symlinks, matching the prior rglob
                # path's ``f.is_file()`` filter.
                if f.suffix in ext_set and f.is_file():
                    eligible.append(f)
    except OSError as exc:
        logger.warning(
            "Repo briefing walk under %s aborted early (%s); using the %d entries found so far",
            repo_path,
            exc,
            len(eligible),
            exc_info=True,
        )
    parts: List[str] = []
    total = 0
    for f in sorted(eligible):
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Repo briefing skipped %s: %s", f, exc)
            continue
        chunk = f"--- {f.relative_to(repo_path)} ---\n{content}\n"
        if total + len(chunk) > max_chars:
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts) if parts else empty


# Filename components and suffixes that may hold credentials/secrets. Files
# matching these are excluded from the content sent to the external review model.
_SENSITIVE_NAMES: frozenset[str] = frozenset(
    {
        "credentials",
        "secrets",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".pgpass",
        ".htpasswd",
        ".envrc",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_SENSITIVE_SUFFIXES: frozenset[str] = frozenset(
    {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".asc", ".ppk"}
)
# A path with any of these as a directory component is treated as secret.
_SENSITIVE_DIR_PARTS: frozenset[str] = frozenset(
    {"secrets", "credentials", ".ssh", ".gnupg", ".aws", ".gpg"}
)
# A file whose stem (name without final suffix) is one of these is secret, so
# stem+ext forms like ``credentials.json`` / ``secrets.py`` are caught.
_SENSITIVE_STEMS: frozenset[str] = frozenset({"credentials", "secret", "secrets"})

# Chunk size for the bounded binary-classification read. Reading in chunks lets a
# NUL byte (binary marker) anywhere short-circuit before the whole file is loaded,
# so a large NUL-free binary cannot exhaust memory.
_READ_CHUNK_BYTES = 65536
# Upper bound on a single reviewable text file. A regular text file under this is
# read in full and passed untruncated (the coordinator segments oversized inputs);
# a file over it is omitted rather than truncated (and rather than loaded whole).
_MAX_REVIEW_FILE_BYTES = 5_000_000


def is_sensitive_path(path: str) -> bool:
    """True when *path* names or sits under a likely secret (``.env``, key, ...).

    Best-effort denylist used to keep secrets out of the content forwarded to the
    external code-review model. It inspects every path component, not just the
    final name: a ``secrets/``/``credentials/``/``.ssh/`` ... directory anywhere
    in the path, a ``credentials``/``secret``/``secrets`` *stem* (so
    ``credentials.json`` and ``app/secrets.py`` match), a known secret basename,
    an anchored ``.env``/``.env.<env>`` (so a regular ``.environment.py`` is not
    excluded), or a key/cert suffix. Over-inclusion (e.g. ``.env.example``) is
    acceptable — losing review of a template is preferable to leaking a key.

    All comparisons are case-folded (the denylist entries are lowercase), so a
    capitalized variant — ``.ENV``, ``ID_RSA``, ``server.PEM`` — cannot bypass
    the filter on a case-sensitive filesystem.
    """
    candidate = Path(path)
    if _SENSITIVE_DIR_PARTS.intersection(p.lower() for p in candidate.parts[:-1]):
        return True
    name = candidate.name.lower()
    if name in _SENSITIVE_NAMES or name == ".env" or name.startswith(".env."):
        return True
    if candidate.stem.lower() in _SENSITIVE_STEMS:
        return True
    return candidate.suffix.lower() in _SENSITIVE_SUFFIXES


# Suffixes that mark a placeholder *template*/example.
_TEMPLATE_SUFFIXES: tuple[str, ...] = (".example", ".sample", ".template", ".dist", ".tmpl")


def _token_is_strong_secret(tok: str) -> bool:
    """True when a single dot-delimited filename token is itself a strong secret signal.

    The per-token counterpart of :func:`is_sensitive_path`'s whole-name checks,
    used to scan *inside* a multi-token name (``.env.id_rsa.example``). A token is
    strong when it is a secret stem (``secrets``), or a known secret basename held
    in :data:`_SENSITIVE_NAMES` either bare (``id_rsa``) or dotted (``.npmrc`` →
    token ``npmrc``), or a key/cert suffix (``.pem`` → token ``pem``).

    Preconditions:
        - none. *tok* is a single token (no ``.``); a multi-token string never
          matches the dotted forms.
    """
    return (
        tok in _SENSITIVE_STEMS
        or tok in _SENSITIVE_NAMES
        or f".{tok}" in _SENSITIVE_NAMES
        or f".{tok}" in _SENSITIVE_SUFFIXES
    )


def is_secret_template_path(path: str) -> bool:
    """True for a placeholder ``.env`` template (``.env.example``/``.env.sample`` ...).

    Used to decide whether a *withheld* (sensitive) path is a harmless example
    that a caller may treat as advisory rather than a hard secret. Only the
    ``.env`` family qualifies, because it is flagged sensitive solely by the
    deliberately over-broad ``.env.<x>`` rule (placeholder env files). A file that
    is sensitive for a *strong* reason — sitting under a secret directory
    (``secrets/``/``.ssh/`` ...), a ``credentials``/``secret`` token, a known
    secret basename (``id_rsa``, ``.npmrc`` ...), or a key/cert suffix *anywhere
    in the name* — is NOT downgraded to a template even when it carries a template
    suffix (so ``secrets/config.sample``, ``credentials.template``,
    ``key.pem.dist``, ``.env.pem.sample``, ``.env.credentials.template`` and
    ``.env.id_rsa.example`` are not treated as harmless).

    The ``.env`` match is anchored on ``.env.`` (consistent with
    :func:`is_sensitive_path`), so a non-env name like ``.environment.sample`` is
    not matched. Every dot-delimited token of the filename is inspected for a
    strong-secret signal, not just the final suffix/stem, so a secret keyword
    embedded *between* ``.env`` and the template suffix cannot slip the check.

    Preconditions:
        - none. Returns False for any non-``.env`` path.
    """
    candidate = Path(path)
    name = candidate.name.lower()
    # Anchored ``.env.`` prefix + a template suffix; nothing else is a template.
    if not (name.startswith(".env.") and name.endswith(_TEMPLATE_SUFFIXES)):
        return False
    # Reject if any *strong* secret signal is present.
    if _SENSITIVE_DIR_PARTS.intersection(p.lower() for p in candidate.parts[:-1]):
        return False
    # Inspect every dot-delimited token of the filename (not just the final
    # suffix/stem) so an embedded key/cert suffix (``.env.pem.sample``), secret
    # stem (``.env.credentials.template``), or known secret basename
    # (``.env.id_rsa.example``, ``.env.npmrc.sample``) is caught.
    return not any(_token_is_strong_secret(tok) for tok in name.split("."))


def strip_surrogates(text: str) -> str:
    """Return *text* with any lone surrogates escaped so it is UTF-8/JSON safe.

    Paths read from git via ``surrogateescape`` (for filenames whose bytes are
    invalid in the locale encoding) carry lone surrogates that raise
    ``UnicodeEncodeError`` when later serialized to JSON or encoded to UTF-8 (for
    example in an LLM HTTP request body). ``backslashreplace`` only rewrites the
    code points that cannot be UTF-8 encoded (the lone surrogates), emitting a
    ``\\uXXXX`` escape for each; every ordinary character — including a *literal*
    backslash in a valid POSIX filename — is left exactly as-is, so the key still
    matches the real on-disk path for downstream fix logic. Plain text is
    unchanged.
    """
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def sanitize_path_for_text(path: str) -> str:
    """Make *path* safe to embed inside a line of model-facing text.

    Git permits filenames containing newlines, tabs, and other control bytes, and
    ``list_changed_and_deleted`` deliberately preserves them (``-z``). Splicing
    such a name verbatim into a bulleted note would let a crafted filename inject
    extra bullets or reviewer "instructions", corrupt a count, or attribute
    findings to invented paths. This first strips lone surrogates
    (:func:`strip_surrogates`) so the result is UTF-8 safe, then escapes every
    non-printable character (``\\n`` → ``\\\\n`` etc.) so the path renders on a
    single line as inert text. Ordinary printable characters — including non-ASCII
    letters in a valid filename — are left unchanged.
    """
    safe = strip_surrogates(path)
    named = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    out: List[str] = []
    for ch in safe:
        if ch == " " or ch.isprintable():
            out.append(ch)
        else:
            out.append(named.get(ch, f"\\x{ord(ch):02x}"))
    return "".join(out)


def _disambiguated_key(result: Dict[str, str], key: str) -> str:
    """Return *key*, or a suffixed variant when it already maps a different path.

    Sanitizing surrogate-bearing paths is not injective: a non-UTF-8 name
    ``a\\xff.py`` and a valid name with literal backslashes can both sanitize to
    the same string. Inserting the second blindly would overwrite the first and
    silently lose its review coverage. When the key is already taken, append a
    numeric suffix until it is free so both files are reviewed under distinct keys.

    Postconditions:
        - The returned key is not currently present in *result*.
    """
    if key not in result:
        return key
    n = 1
    while f"{key}~{n}" in result:
        n += 1
    return f"{key}~{n}"


def read_files_as_dict(
    repo_path: Path,
    paths: Iterable[str],
    extensions: Optional[List[str]] = None,
    *,
    omitted: Optional[List[str]] = None,
    key_to_path: Optional[Dict[str, str]] = None,
    symlinked: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Read *paths* under *repo_path* into a ``{path: content}`` mapping.

    Parameters
    ----------
    repo_path:
        Root the paths are resolved against.
    paths:
        Repo-relative paths to read (e.g. the changed paths from
        ``list_changed_and_deleted``).
    extensions:
        When given, only paths whose suffix is in this list are included.
        ``None`` means no extension filter (so files without a code suffix,
        such as ``Dockerfile`` or ``requirements.txt``, pass through).
    omitted:
        Optional list the function appends to. Every requested path that is
        dropped for a reason *other* than the ``extensions`` filter — outside the
        repo, binary, a directory/submodule, missing, or otherwise unreadable —
        is appended (its original repo-relative string). The caller can then
        surface these as a blocking review note instead of silently approving a
        change set with a task-owned file that was never examined. The
        ``extensions`` filter is a deliberate caller scoping choice, so
        extension-filtered paths are *not* reported as omissions.
    symlinked:
        Optional list the function appends to. Every requested path detected as a
        symlink on disk (the same paths represented by the ``# symlink -> ...``
        marker) is appended (its original repo-relative string), making this the
        single place symlinks are detected. A caller that must route symlinks to
        manual review (their pointed-to content is never examined) can reuse this
        instead of re-statting every path itself.

    Preconditions:
        - *repo_path* is an existing directory (the task's working tree). On
          Python 3.6+ ``resolve()`` is non-strict so a missing root does not
          raise, but a non-existent root yields an empty mapping rather than
          meaningful review input.
        - *paths* are repo-relative; the caller has already scoped them.
    Postconditions:
        - Returns a mapping in the iteration order of *paths*, skipping any path
          that is filtered out by *extensions*, escapes *repo_path* (an absolute
          path or one containing ``..``), is binary, or is missing/unreadable.
        - When *omitted* is provided, it gains one entry per non-extension skip,
          so ``set(paths)`` minus extension-filtered equals the reviewed keys'
          originals plus *omitted* (no path vanishes without a trace).
        - When *key_to_path* is provided, it gains a ``review_key -> original_path``
          entry for every included file, so a caller that receives a review
          finding tagged with a (display-safe, possibly suffixed) key can
          translate it back to the real on-disk path the fixer must edit.
        - A symlink is represented by its link target text (``# symlink -> ...``)
          and never dereferenced, so the target's unrelated content is not
          mislabeled under the link path and a link pointing outside the repo
          cannot leak content. When *symlinked* is provided, the link's original
          path is also appended to it, so a caller need not re-stat to find them.
        - A text file is read in full and passed untruncated (the review
          coordinator segments oversized inputs itself); only a binary *sniff*
          prefix is bounded, so a huge binary artifact is detected and skipped
          before any full read.
        - Text is decoded as UTF-8 with ``errors="replace"`` (matching
          ``read_repo_code``) so a legacy/non-UTF-8 text file is reviewed rather
          than dropped; binary content (a NUL byte in the read prefix) is omitted
          rather than decoded into gibberish.
        - Result keys (and the symlink-target marker) are run through
          :func:`sanitize_path_for_text`, so a non-UTF-8 *or* control-character
          filename cannot crash downstream UTF-8/JSON serialization or inject
          prompt text via the coordinator's file label. The file content is still
          read from the original (surrogate-bearing) path, and *key_to_path* keeps
          the key→original mapping. Sanitizing is not injective, so when two
          distinct paths sanitize to the same key the later one gets a numeric
          suffix (it never overwrites the first) — every reviewed path keeps a
          distinct entry.
        - Never reads a file outside *repo_path*: keys may come from untrusted
          agent output, so containment is enforced before any read.
    """
    repo_root = repo_path.resolve()
    result: Dict[str, str] = {}
    for rel_path in paths:
        candidate = Path(rel_path)
        if extensions is not None and candidate.suffix not in extensions:
            continue
        full_path = repo_root / candidate
        try:
            # Lexical containment first — no symlink following — so an absolute or
            # ``..`` key from untrusted agent output is rejected up front.
            lexical = Path(os.path.normpath(full_path))
            if lexical != repo_root and repo_root not in lexical.parents:
                logger.debug("read_files_as_dict: skipping %s (outside repo)", rel_path)
                if omitted is not None:
                    omitted.append(rel_path)
                continue
            # A symlink is reported by its target, never dereferenced (which would
            # mislabel the target's content under the link or escape the repo).
            if full_path.is_symlink():
                key = _disambiguated_key(result, sanitize_path_for_text(rel_path))
                # Escape the target (it can carry control bytes) but keep a real
                # trailing newline so the marker renders as one line.
                result[key] = f"# symlink -> {sanitize_path_for_text(os.readlink(full_path))}\n"
                if key_to_path is not None:
                    key_to_path[key] = rel_path
                if symlinked is not None:
                    symlinked.append(rel_path)
                continue
            # Non-symlink: resolve (following any intra-repo parent links) and
            # re-check containment before reading.
            resolved = full_path.resolve()
            if repo_root not in resolved.parents:
                logger.debug("read_files_as_dict: skipping %s (resolves outside repo)", rel_path)
                if omitted is not None:
                    omitted.append(rel_path)
                continue
            # Require a *regular* file before opening: a FIFO/socket/device node
            # (possible among untracked entries on the whole-repo fallback) would
            # block ``open`` indefinitely waiting for a writer, hanging the review.
            # A directory (a changed submodule gitlink) is likewise not a regular
            # file. ``is_file()`` stats without opening, so neither blocks.
            if not resolved.is_file():
                logger.debug("read_files_as_dict: skipping %s (not a regular file)", rel_path)
                if omitted is not None:
                    omitted.append(rel_path)
                continue
            # Read in bounded chunks, bailing on the first NUL (binary) or once
            # the accumulated text passes _MAX_REVIEW_FILE_BYTES. This keeps memory
            # bounded even for a large NUL-free binary (archive/model/media) whose
            # first bytes look textual — reading the whole remainder up front could
            # exhaust the worker. A regular text file under the cap is still read in
            # full and passed untruncated (the coordinator segments oversized
            # inputs); a file over the cap is omitted rather than truncated.
            buf = bytearray()
            binary_or_oversized = False
            with open(resolved, "rb") as handle:
                while True:
                    chunk = handle.read(_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    if b"\x00" in chunk:
                        binary_or_oversized = True
                        break
                    buf += chunk
                    if len(buf) > _MAX_REVIEW_FILE_BYTES:
                        binary_or_oversized = True
                        break
            if binary_or_oversized:
                logger.debug("read_files_as_dict: skipping %s (binary or oversized)", rel_path)
                if omitted is not None:
                    omitted.append(rel_path)
                continue
            text = bytes(buf).decode("utf-8", errors="replace")
            key = _disambiguated_key(result, sanitize_path_for_text(rel_path))
            result[key] = text
            if key_to_path is not None:
                key_to_path[key] = rel_path
        except (OSError, RuntimeError, ValueError) as exc:
            logger.debug("read_files_as_dict: skipping %s (%s)", rel_path, exc)
            if omitted is not None:
                omitted.append(rel_path)
            continue
    return result


def read_repo_files_as_dict(
    repo_path: Path,
    *,
    key_to_path: Optional[Dict[str, str]] = None,
    omitted: Optional[List[str]] = None,
    sensitive_skipped: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Read every reviewable text file under *repo_path* into a ``{path: content}`` map.

    The whole-repo, all-file-types counterpart of :func:`read_files_as_dict`, used
    for the fail-closed fallback (when no trusted task-scoped change set exists).
    Walks the repo excluding build/VCS dirs (:data:`REPO_INSPECT_EXCLUDE_DIRS`
    plus ``.git``), drops sensitive paths, and reuses ``read_files_as_dict``'s
    binary/size/containment handling — so the fallback reviews the same breadth
    (config, migrations, JS, docs, ...) the normal unfiltered path does, not just
    ``.py``/``.java``.

    Excluded directories are pruned *during* the walk (``os.walk`` with in-place
    ``dirnames`` filtering), not enumerated-then-discarded, so a checkout with a
    huge ``node_modules``/``.venv``/``.git`` is never descended into — the fallback
    cannot stall enumerating millions of dependency files precisely in the repos
    these exclusions protect.

    Postconditions:
        - Returns ``{}`` for an empty repo; never raises for a missing file.
        - When *omitted* / *sensitive_skipped* are provided, they collect the
          paths dropped as unreadable/binary and as sensitive (respectively), so
          the caller can surface them rather than approving a whole-repo review
          that silently excluded files. *key_to_path* maps review keys back to
          real paths.
    """
    always_exclude = REPO_INSPECT_EXCLUDE_DIRS | {".git"}
    repo_root = repo_path.resolve()
    paths: List[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # A symlink to a directory lands in *dirnames* and, with the default
        # followlinks=False, os.walk neither descends it nor emits it via
        # *filenames* — so it would vanish from the review. Surface it as a path
        # (read_files_as_dict represents it by its link target, never dereferenced)
        # before pruning excluded dirs from the traversal set.
        for d in dirnames:
            if d in always_exclude:
                continue  # an excluded dir (even a symlink named .git/node_modules)
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                rel = os.path.relpath(full, repo_root)
                if is_sensitive_path(rel):
                    if sensitive_skipped is not None:
                        sensitive_skipped.append(rel)
                else:
                    paths.append(rel)
        # Prune excluded directories in place so os.walk never descends into them.
        dirnames[:] = [d for d in dirnames if d not in always_exclude]
        for name in filenames:
            # In a linked worktree or submodule, ``.git`` is a regular *file*
            # (``gitdir: ...``), not a directory, so the dirname prune above misses
            # it; skip that one file so the checkout's gitdir/host-path metadata
            # never reaches the review model. Only ``.git`` is matched here (not the
            # whole dir-exclude set) so a legitimate regular file that merely shares
            # an excluded *directory* name (e.g. a script named ``dist``) is still
            # reviewed.
            if name == ".git":
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), repo_root)
            if is_sensitive_path(rel):
                if sensitive_skipped is not None:
                    sensitive_skipped.append(rel)
                continue
            paths.append(rel)
    return read_files_as_dict(
        repo_path, paths, extensions=None, omitted=omitted, key_to_path=key_to_path
    )


def find_repo_files(
    repo_path: Path,
    *,
    suffixes: Iterable[str] = (),
    names: Iterable[str] = (),
    exclude_dirs: Iterable[str] = REPO_INSPECT_EXCLUDE_DIRS,
) -> List[Path]:
    """Return regular files under *repo_path* matching *suffixes* and/or *names*.

    The pruned-walk counterpart of :func:`read_repo_code_budgeted` for callers
    that need *paths* (not concatenated content): stack-language detection
    (``_detect_language``) and the build-fix file collector. A streamed
    ``os.walk`` prunes excluded directory names in place via ``dirnames[:]``, so
    a checkout with a huge ``node_modules`` / ``.git`` / ``.venv`` is never
    descended into — the same I/O discipline ``read_repo_code_budgeted``
    established, replacing the ``rglob`` calls that materialized those excluded
    subtrees before filtering. *exclude_dirs* itself is copied into a new
    ``frozenset`` and is never mutated.

    Preconditions:
        - *repo_path* is a ``Path`` (may not exist). A non-directory yields ``[]``
          rather than raising, so best-effort detectors (``_detect_language``)
          can call this unconditionally.
        - *suffixes* are file suffixes including the leading dot (``.py``);
          *names* are exact basenames (``pom.xml``). Either may be empty (no
          filter on that axis). A file matches when it satisfies either axis.
        - *exclude_dirs* may be any iterable of directory names, including a
          ``frozenset``. It is copied; walk pruning mutates ``os.walk``'s
          ``dirnames`` list only.
    Postconditions:
        - Returns paths for regular files matching a requested suffix or name,
          in walk order; ``[]`` when none match or *repo_path* is not a
          directory. Never raises: a mid-walk ``OSError`` degrades to the entries
          found so far, mirroring :func:`read_repo_code_budgeted`.
    """
    try:
        if not repo_path.is_dir():
            return []
    except OSError:
        return []
    suffix_set = frozenset(suffixes)
    name_set = frozenset(names)
    excl_set = frozenset(exclude_dirs)
    matched: List[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(repo_path):
            dirnames[:] = [d for d in dirnames if d not in excl_set]
            for name in filenames:
                f = Path(dirpath) / name
                # is_file() guards against special files (FIFO/socket/device) the
                # way read_repo_code_budgeted's does; a name match alone is not
                # enough since a dir could be named e.g. ``pom.xml``.
                if not f.is_file():
                    continue
                if (suffix_set and f.suffix in suffix_set) or (name_set and name in name_set):
                    matched.append(f)
    except OSError as exc:
        logger.warning(
            "find_repo_files walk under %s aborted early (%s); using %d entries found so far",
            repo_path,
            exc,
            len(matched),
            exc_info=True,
        )
    return matched


def truncate_for_context(
    text: str, max_chars: int, llm: object = None, content_description: str = "content"
) -> str:
    """Compact *text* with LLM when over budget; pass full text when no LLM available."""
    if not text or len(text) <= max_chars:
        return text or ""
    if llm is not None:
        from llm_service import compact_text

        return compact_text(text, max_chars, llm, content_description)
    return text


def int_env(name: str, default: int, min_val: int = 1) -> int:
    """Read an integer from environment variable *name*, clamped to *min_val*."""
    try:
        return max(min_val, int(os.environ.get(name) or str(default)))
    except ValueError:
        return default
