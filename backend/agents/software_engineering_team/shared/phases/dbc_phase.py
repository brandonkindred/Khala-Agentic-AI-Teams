"""Design by Contract comments self-review phase for the gated execution loop.

Two layers, mirroring ``documentation_phase.py``'s factoring:

- :func:`run_dbc_comments_review` -- the reusable, team-agnostic concrete
  review: builds a :class:`DbcCommentsInput` from a dict of files, runs a
  freshly constructed :class:`DbcCommentsAgent` (it self-resolves its own LLM
  client, so unlike documentation self-review this needs no per-team Strands
  ``Agent``/model wrapper -- it can live here, shared, rather than in each
  team's ``phases/review.py``).
- :func:`_run_dbc_self_review` -- the gated-loop phase orchestrator, calling
  ``gate_config.run_dbc_self_review(...)`` and applying the write/revert
  machinery.

Neither function is called from ``run_gated_execution_impl`` yet -- wiring
that call (and each team's ``GATE_CONFIG.run_dbc_self_review`` assignment) is
sibling work. This module is self-contained and directly tested.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    NamedTuple,
    Optional,
)

from shared.dev_models.models import SystemArchitecture, Task
from software_engineering_team.shared.repo_writer import (
    UnsafeRepoPathError,
    resolve_safe_repo_path,
    write_repo_text_files,
)
from software_engineering_team.technical_writers.dbc_comments_agent.agent import DbcCommentsAgent
from software_engineering_team.technical_writers.dbc_comments_agent.models import (
    DbcCommentsInput,
    DbcCommentsOutput,
    DbcCommentsStatus,
)

if TYPE_CHECKING:
    from software_engineering_team.shared.phases.execution import ReviewDependencies

logger = logging.getLogger(__name__)

# Mirrors (deliberately more permissive than) DbcCommentsAgent's own
# chunking._FILE_HEADER_PATTERN: a whole line of the form "### path ###".
# DbcCommentsAgent's concatenated-code transport has no per-file input, so a
# reviewed file whose own content contains such a line is inherently
# ambiguous to its parser -- it cannot tell that line apart from a real chunk
# boundary. If the line's path collides with another reviewed file's real
# path, the parser splits that real file's content into two spurious blocks
# and the agent's merge step can attach an insertion derived from one file's
# content to a different, unrelated file. There is no way to safely
# distinguish this case after the fact, so a matching file is excluded from
# review entirely up front, before concatenation.
_HEADER_LIKE_LINE = re.compile(r"^###[ \t]+\S[^\n]*[ \t]+###[ \t]*$", re.MULTILINE)

# Pruned when snapshotting the worktree so a later build-verifier revert /
# success-sync does not walk dependency, VCS, artifact, venv, or pytest-cache
# trees. The production fixer refuses writes into every name in this set (see
# ``_REPAIR_SKIP_WRITE_DIRS`` in ``build_fix``) so a repair cannot land in a
# tree this snapshot would not restore. Virtualenvs are pruned so a
# per-microtask snapshot cannot load hundreds of MB of site-packages.
# ``.pytest_cache`` is pruned so a passing pytest run cannot be classified as
# an LLM repair.
_WORKTREE_SNAPSHOT_EXCLUDE_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        "build",
        "__pycache__",
        ".angular",
        "venv",
        ".venv",
        ".pytest_cache",
    }
)


def run_dbc_comments_review(
    *,
    code: Dict[str, str],
    language: str = "python",
    task_description: str = "",
    architecture: Optional[SystemArchitecture] = None,
    detail_callback: Optional[Callable[[str], None]] = None,
) -> DbcCommentsOutput:
    """Run a Design by Contract comments review over a microtask's files.

    Preconditions:
        ``code`` maps relative file paths to their text content (the same
        shape as ``microtask_files`` elsewhere in the gated loop) -- not a
        pre-concatenated string. An empty dict is valid.

    Postconditions:
        A file in ``code`` is excluded before concatenation (see
        ``_HEADER_LIKE_LINE``) when its own content contains a ``### path
        ###``-shaped line, or its path contains a newline (letting the path
        inject its own extra header line into the rendered ``### {path}
        ###`` header), or its path has leading/trailing whitespace (which
        ``parse_code_into_file_blocks`` strips when parsing headers back out,
        so a padded path and its unpadded twin -- or a lone padded path and
        this function's own membership bookkeeping downstream -- would
        silently collide or mismatch). Such a file cannot be safely
        represented in DbcCommentsAgent's concatenated-code transport, so it
        is skipped rather than risking an insertion from a spurious parsed
        block being merged into an unrelated file. The remaining files are
        concatenated
        into the ``### path ###``-headered format ``DbcCommentsAgent``/
        ``parse_code_into_file_blocks`` expect (one
        ``"### {path} ###\\n{content}"`` block per file, joined with
        ``"\\n\\n"``, matching ``DbcChunk.content``'s own rendering), then
        reviewed via a freshly constructed ``DbcCommentsAgent()`` (it
        self-resolves its own LLM client). ``detail_callback``, if given, is
        invoked with a short human-readable string for each status update the
        agent reports.

    Raises:
        Nothing. Constructing ``DbcCommentsAgent()`` and calling ``.run()``
        are both wrapped in a single ``try/except``; any exception is logged
        and converted into a safe fallback ``DbcCommentsOutput``
        (``already_compliant=False``, an explanatory ``summary``) rather than
        propagated -- this holds regardless of ``DbcCommentsAgent.run()``'s
        own "never raises" contract, as defense in depth for this layer too.
    """
    safe_code = {
        path: content
        for path, content in code.items()
        if path == path.strip() and "\n" not in path and not _HEADER_LIKE_LINE.search(content)
    }
    excluded = code.keys() - safe_code.keys()
    if excluded:
        logger.warning(
            "DbC comments review: excluding file(s) %s -- their path or content contains a "
            "'### path ###'-shaped line, an embedded newline, or leading/trailing whitespace "
            "that would be misread or normalized away by the chunk parser",
            sorted(excluded),
        )

    concatenated = "\n\n".join(f"### {path} ###\n{content}" for path, content in safe_code.items())

    def _on_status(status: DbcCommentsStatus, detail: str = "") -> None:
        if detail_callback:
            detail_callback(detail or status.value)

    try:
        dbc_agent = DbcCommentsAgent()
        dbc_input = DbcCommentsInput(
            code=concatenated,
            language=language,
            task_description=task_description,
            architecture=architecture,
        )
        return dbc_agent.run(dbc_input, on_status=_on_status if detail_callback else None)
    except Exception as e:  # noqa: BLE001 -- see docstring: this call never raises.
        logger.warning("DbC comments review failed, treating as non-compliant: %s", e)
        return DbcCommentsOutput(
            already_compliant=False,
            summary=f"DbC comments review could not run: {e}",
        )


def _restore_dict_entry(d: Dict[str, str], key: str, prior: Optional[str]) -> None:
    """Restore ``d[key]`` to ``prior``, or remove it entirely when ``prior`` is ``None``.

    Postconditions:
        ``d[key] == prior`` when ``prior is not None``; ``key not in d`` otherwise.
    """
    if prior is None:
        d.pop(key, None)
    else:
        d[key] = prior


def _clear_path(full_path: Path) -> None:
    """Remove ``full_path`` whether it is a file, symlink, or directory.

    Preconditions:
        ``full_path`` is the location to clear; it need not exist.

    Postconditions:
        ``full_path`` does not exist. A symlink is unlinked without following
        it. A directory tree is removed with ``shutil.rmtree``. Missing paths
        are ignored.
    """
    if full_path.is_symlink() or full_path.is_file():
        full_path.unlink(missing_ok=True)
        return
    if full_path.is_dir():
        shutil.rmtree(full_path)
        return
    full_path.unlink(missing_ok=True)


def _revert_disk(prior_disk: Dict[Path, Optional[bytes]]) -> None:
    """Best-effort restore of each snapshotted path to its prior on-disk bytes.

    Postconditions:
        Each path with ``prior_bytes is None`` is deleted (it did not exist
        before); each other path is restored to ``prior_bytes``. A current
        symlink, directory, or other non-regular path at that location is
        removed first so ``write_bytes`` cannot follow a verifier-planted
        link or fail on ``IsADirectoryError``. Ancestors are processed before
        descendants so a replacement directory-symlink is cleared before a
        nested file restore. An ordinary filesystem failure is logged and
        skipped per-path rather than raised -- this is already the fallback
        path of a "never fails" phase, so it must not itself introduce a new
        way to fail loud.
    """
    ordered = sorted(prior_disk.items(), key=lambda item: (len(item[0].parts), str(item[0])))
    for full_path, prior_bytes in ordered:
        try:
            _clear_path(full_path)
            if prior_bytes is not None:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_bytes(prior_bytes)
        except OSError as exc:
            logger.warning("Failed to revert %s to its prior state (ignored): %s", full_path, exc)


def _restore_symlinks(symlinks: Dict[Path, str]) -> None:
    """Recreate snapshotted symlinks at their recorded targets.

    Preconditions:
        ``symlinks`` maps symlink paths to ``os.readlink`` target strings.

    Postconditions:
        Each path is a symlink whose ``os.readlink`` value is ``target``.
        Whatever currently occupies the path is cleared first. Restore
        failures are logged and skipped per-path.
    """
    for full_path, target in symlinks.items():
        try:
            _clear_path(full_path)
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.symlink_to(target)
        except OSError as exc:
            logger.warning("Failed to restore symlink %s (ignored): %s", full_path, exc)


def _raise_walk_error(err: OSError) -> None:
    """Re-raise a walk error so ``os.walk`` cannot swallow a partial scan.

    Postconditions:
        Always raises ``err``.
    """
    raise err


def _iter_worktree_files(
    root: Path, *, strict: bool = True, include_symlinks: bool = False
) -> Iterator[Path]:
    """Yield resolved regular-file paths under ``root``, pruning excluded dirs.

    Preconditions:
        ``root`` is an existing directory.

    Postconditions:
        Directory symlinks are not followed (``os.walk`` default). File and
        directory symlinks are skipped unless ``include_symlinks`` is true,
        in which case they are yielded unfollowed (not ``resolve()``'d) and
        directory links are not descended into. When ``strict`` is true, an
        ``OSError`` mid-walk is raised to the caller so a snapshot cannot
        proceed from a partial scan. When ``strict`` is false, walk errors
        are skipped (best-effort enumeration for revert).
    """
    onerror = _raise_walk_error if strict else None
    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        kept: list[str] = []
        for d in dirnames:
            child = Path(dirpath, d)
            if child.is_symlink():
                if include_symlinks:
                    yield child
                continue
            if d not in _WORKTREE_SNAPSHOT_EXCLUDE_DIRS:
                kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            path = Path(dirpath, name)
            if path.is_symlink() or not path.is_file():
                if include_symlinks and path.is_symlink():
                    yield path
                continue
            yield path.resolve()


class _WorktreeSnapshot(NamedTuple):
    """Pre-verify worktree inventory: regular-file bytes and symlink targets."""

    files: Dict[Path, bytes]
    symlinks: Dict[Path, str]


def _snapshot_worktree(root: Path) -> _WorktreeSnapshot:
    """Read every non-excluded regular file under ``root``.

    Postconditions:
        ``files`` maps resolved regular-file paths to their bytes. ``symlinks``
        maps file- and directory-symlink paths (unfollowed) to their
        ``os.readlink`` targets so revert can restore a deleted or retargeted
        link. Raises ``OSError`` if a walk, read, or ``readlink`` fails.
    """
    files: Dict[Path, bytes] = {}
    links: Dict[Path, str] = {}
    for path in _iter_worktree_files(root, include_symlinks=True):
        if path.is_symlink():
            links[path] = os.readlink(path)
            continue
        files[path] = path.read_bytes()
    return _WorktreeSnapshot(files=files, symlinks=links)


def _revert_verifier_side_effects(
    *,
    root: Path,
    prior_disk: Dict[Path, Optional[bytes]],
    pre_verify_disk: Dict[Path, bytes],
    pre_verify_symlinks: Dict[Path, str],
) -> None:
    """Restore pre-DbC DbC files and undo any other verifier worktree writes.

    Postconditions:
        ``prior_disk`` paths are restored to their pre-DbC bytes (or deleted
        if they did not exist). Every other path present in
        ``pre_verify_disk`` is restored to its post-DbC / pre-verifier bytes.
        Regular files and file/directory-symlinks that exist now under
        ``root`` but were in neither map nor ``pre_verify_symlinks``
        (verifier-created) are deleted. Snapshotted symlinks are recreated
        at their recorded targets. Restore failures are swallowed by
        ``_revert_disk`` / ``_restore_symlinks``.
    """
    revert_map: Dict[Path, Optional[bytes]] = dict(pre_verify_disk)
    revert_map.update(prior_disk)
    current = _list_worktree_files_for_revert(root)
    for path in current:
        if path not in revert_map and path not in pre_verify_symlinks:
            revert_map[path] = None
    _revert_disk(revert_map)
    _restore_symlinks(pre_verify_symlinks)


def _list_worktree_files_for_revert(root: Path) -> list[Path]:
    """Enumerate worktree files for revert, retrying then falling back.

    Preconditions:
        ``root`` is an existing directory.

    Postconditions:
        Returns the strict walk result when it succeeds. On ``OSError``,
        retries once; if that also fails, returns a best-effort walk
        (``strict=False``) so verifier-created files in accessible dirs are
        still marked for delete rather than left on disk.
    """
    try:
        return list(_iter_worktree_files(root, include_symlinks=True))
    except OSError as exc:
        logger.warning("Could not enumerate worktree while reverting verifier edits: %s", exc)
    try:
        return list(_iter_worktree_files(root, include_symlinks=True))
    except OSError as exc:
        logger.warning(
            "Retry failed; using best-effort walk while reverting verifier edits: %s",
            exc,
        )
        return list(_iter_worktree_files(root, strict=False, include_symlinks=True))


def _posix_rel(root: Path, path: Path) -> Optional[str]:
    """Return ``path`` relative to ``root`` as a POSIX string, or ``None``.

    Postconditions:
        ``None`` when ``path`` is not under ``root``.
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def _sync_verifier_repairs_into_maps(
    *,
    root: Path,
    pre_verify_disk: Dict[Path, bytes],
    microtask_files: Dict[str, str],
    all_files: Dict[str, str],
    mt: Any,
) -> bool:
    """Copy verifier-mutated worktree files into the in-memory file maps.

    Postconditions:
        Every non-excluded regular file under ``root`` whose bytes differ from
        ``pre_verify_disk`` (or that is absent from it) is decoded as UTF-8
        and written into ``microtask_files``, ``all_files``, and
        ``mt.output_files``. Unchanged files are left alone. Paths present in
        ``pre_verify_disk`` but absent from the current walk (verifier
        deletions) are removed from those maps. A per-file read or decode
        failure is logged and skipped. Returns ``False`` (without mutating
        the maps) when the walk itself fails, so the caller can treat the
        verify as unsuccessful and revert. Returns ``True`` when the walk
        completed.
    """
    try:
        current = list(_iter_worktree_files(root))
    except OSError as exc:
        logger.warning("Could not enumerate worktree while syncing verifier repairs: %s", exc)
        return False

    output_files = (
        mt.output_files if isinstance(getattr(mt, "output_files", None), dict) else microtask_files
    )
    for path in current:
        prior = pre_verify_disk.get(path)
        try:
            now = path.read_bytes()
        except OSError as exc:
            logger.warning("Could not read %s while syncing verifier repairs: %s", path, exc)
            continue
        if prior is not None and prior == now:
            continue
        rel = _posix_rel(root, path)
        if rel is None:
            continue
        try:
            text = now.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Skipping non-UTF-8 verifier repair at %s", path)
            continue
        microtask_files[rel] = text
        all_files[rel] = text
        output_files[rel] = text
    current_set = set(current)
    for path in pre_verify_disk:
        if path in current_set:
            continue
        rel = _posix_rel(root, path)
        if rel is None:
            continue
        microtask_files.pop(rel, None)
        all_files.pop(rel, None)
        output_files.pop(rel, None)
    mt.output_files = output_files
    return True


def _run_dbc_self_review(
    *,
    gate_config: Any,
    task: Task,
    task_id: str,
    mt: Any,
    microtask_files: Dict[str, str],
    repo_path: Path,
    all_files: Dict[str, str],
    architecture: Optional[SystemArchitecture],
    language: str,
    deps: "ReviewDependencies",
    build_verify_label: str,
    progress_callback: Optional[Callable[[int, int, int, str, str, str], None]],
    current_idx: int,
    completed_ids: set,
    total: int,
    detail_cb: Callable[[str, int, str], None],
) -> None:
    """Run a microtask's Design by Contract comments self-review (never fails).

    Intended to run immediately before the Documentation phase for the same
    microtask, so ``microtask_files`` reflects any DbC insertions by the time
    Documentation reads it as its own ``code_files`` argument. ``gate_config``
    is duck-typed here (``Any``): ``GatedExecutionConfig`` does not yet carry
    a ``run_dbc_self_review`` field -- adding it and wiring this function into
    ``run_gated_execution_impl`` is sibling work.

    Preconditions:
        ``microtask_files`` reflects the last review-gate-accepted write.
        ``completed_ids`` is read-only here (only its length is read, for the
        progress-callback tick) -- this phase does not add ``mt.id`` to it and
        does not set ``mt.status``; both remain the Documentation phase's
        responsibility.

    Postconditions:
        ``microtask_files``, ``all_files``, and ``mt.output_files`` gain any
        DbC comment insertions the review produced, written under
        ``repo_path`` -- restricted to paths that were keys of
        ``microtask_files`` to begin with; a result path DbC invented (e.g.
        from a header-shaped comment line inside a reviewed file being
        misread as a chunk boundary) is discarded rather than written. If
        ``deps.build_verifier`` is set and reports failure
        for the post-insertion tree, the file(s) this phase's DbC result
        touched are reverted -- on disk (prior content restored, or the file
        deleted if it was newly created) and in ``microtask_files``/
        ``all_files``/``mt.output_files`` (prior value restored, or the key
        popped if it did not exist before). Any additional worktree writes
        the verifier itself made (e.g. production ``_run_build_verification``
        repairing files via ``_try_build_fix_one_at_a_time`` before it knows
        whether verification will pass) are also restored or deleted so they
        cannot leak into a later commit; files the verifier did not touch
        remain unchanged. If the verifier reports success after mutating the
        worktree, those repaired/created files are copied into
        ``microtask_files``/``all_files``/``mt.output_files`` so a later
        Documentation pass cannot overwrite disk from stale map contents. A
        ``gate_config.run_dbc_self_review`` exception, an
        unsafe write path, an ordinary filesystem or encoding failure while snapshotting
        or writing (e.g. a full disk or revoked permissions), or a
        build-verifier exception are all logged and skipped/reverted rather
        than propagated -- this phase never raises or
        fails the microtask.
    """
    if progress_callback:
        progress_callback(
            current_idx,
            len(completed_ids),
            total,
            mt.title or mt.id,
            "dbc",
            "Starting DbC comments self-review...",
        )

    try:
        dbc_result = gate_config.run_dbc_self_review(
            code=microtask_files,
            language=language,
            task_description=task.description or "",
            architecture=architecture,
            detail_callback=lambda d: detail_cb(d, current_idx, "dbc"),
        )
    except Exception as e:
        logger.warning(
            "[%s] Microtask %s: DbC self-review call failed, skipping: %s", task_id, mt.id, e
        )
        return

    raw_dbc_files: Dict[str, str] = getattr(dbc_result, "files", None) or {}
    # A reviewed file's own content can contain a line that happens to match
    # the "### path ###" header DbcCommentsAgent's chunk parser looks for
    # (e.g. a section-divider comment) -- parse_code_into_file_blocks then
    # misreads it as a header for a file that was never actually reviewed,
    # and the merge can attach an insertion to that spurious path. Only
    # write back paths that were actually part of what we asked to review.
    dbc_files = {
        path: content for path, content in raw_dbc_files.items() if path in microtask_files
    }
    if len(dbc_files) != len(raw_dbc_files):
        logger.warning(
            "[%s] Microtask %s: DbC self-review returned %d file(s) not among the reviewed "
            "files, discarding: %s",
            task_id,
            mt.id,
            len(raw_dbc_files) - len(dbc_files),
            sorted(set(raw_dbc_files) - set(dbc_files)),
        )
    if not dbc_files:
        logger.info("[%s] Microtask %s: DbC self-review made no changes", task_id, mt.id)
        return

    root = Path(repo_path).resolve()
    output_files = mt.output_files if isinstance(getattr(mt, "output_files", None), dict) else {}

    # Snapshot prior state for exactly the touched keys, before any write.
    # Deliberately not rollback.py's whole-microtask _MicrotaskRollback
    # machinery (its revert wholesale-clears mt.output_files) -- a
    # build-verifier failure here must revert only the DbC-touched keys in
    # the in-memory maps. Disk restore also undoes extra files the verifier
    # itself wrote (repair-loop side effects).
    prior_microtask: Dict[str, Optional[str]] = {}
    prior_all: Dict[str, Optional[str]] = {}
    prior_output: Dict[str, Optional[str]] = {}
    prior_disk: Dict[Path, Optional[bytes]] = {}

    try:
        for rel_path in dbc_files:
            prior_microtask[rel_path] = microtask_files.get(rel_path)
            prior_all[rel_path] = all_files.get(rel_path)
            prior_output[rel_path] = output_files.get(rel_path)
            try:
                full_path = resolve_safe_repo_path(root, rel_path)
            except UnsafeRepoPathError:
                continue  # write_repo_text_files rejects the whole batch below.
            prior_disk[full_path] = full_path.read_bytes() if full_path.exists() else None
    except OSError as exc:
        logger.warning(
            "[%s] Microtask %s: could not snapshot prior DbC file state, skipping: %s",
            task_id,
            mt.id,
            exc,
        )
        return  # No write attempted -- nothing to revert.

    try:
        write_repo_text_files(repo_path, dbc_files)
    except UnsafeRepoPathError as exc:
        logger.warning(
            "[%s] Microtask %s: unsafe DbC comments path rejected, skipping: %s",
            task_id,
            mt.id,
            exc,
        )
        return  # Nothing was written (write_repo_text_files is all-or-nothing).
    except (OSError, UnicodeEncodeError) as exc:
        logger.warning(
            "[%s] Microtask %s: DbC comments write failed (%s), reverting any partial write",
            task_id,
            mt.id,
            exc,
        )
        # write_repo_text_files writes file-by-file after validating every
        # path, so a mid-batch OSError (disk full, permission revoked, a
        # path replaced by a directory) can leave some files written and
        # others not -- restore every snapshotted path regardless, since
        # the dicts were never updated (see below) there is nothing to
        # revert there.
        _revert_disk(prior_disk)
        return

    microtask_files.update(dbc_files)
    all_files.update(dbc_files)
    mt.output_files = microtask_files

    if deps.build_verifier is not None:
        pre_verify: Optional[_WorktreeSnapshot]
        try:
            pre_verify = _snapshot_worktree(root)
        except OSError as exc:
            logger.warning(
                "[%s] Microtask %s: could not snapshot worktree before DbC build "
                "verify, skipping verify: %s",
                task_id,
                mt.id,
                exc,
            )
            pre_verify = None

        if pre_verify is not None:

            def _revert_after_verify() -> None:
                for rel_path in dbc_files:
                    _restore_dict_entry(microtask_files, rel_path, prior_microtask[rel_path])
                    _restore_dict_entry(all_files, rel_path, prior_all[rel_path])
                    _restore_dict_entry(output_files, rel_path, prior_output[rel_path])
                mt.output_files = output_files
                _revert_verifier_side_effects(
                    root=root,
                    prior_disk=prior_disk,
                    pre_verify_disk=pre_verify.files,
                    pre_verify_symlinks=pre_verify.symlinks,
                )

            try:
                ok, msg = deps.build_verifier(repo_path, build_verify_label, task_id)
            except Exception as exc:
                logger.warning(
                    "[%s] Microtask %s: DbC build verifier raised: %s", task_id, mt.id, exc
                )
                ok, msg = False, str(exc)

            if not ok:
                logger.warning(
                    "[%s] Microtask %s: build failed after DbC comments (%s), reverting %d file(s)",
                    task_id,
                    mt.id,
                    msg,
                    len(dbc_files),
                )
                _revert_after_verify()
                return

            if not _sync_verifier_repairs_into_maps(
                root=root,
                pre_verify_disk=pre_verify.files,
                microtask_files=microtask_files,
                all_files=all_files,
                mt=mt,
            ):
                logger.warning(
                    "[%s] Microtask %s: could not sync verifier repairs into maps, "
                    "reverting %d file(s)",
                    task_id,
                    mt.id,
                    len(dbc_files),
                )
                _revert_after_verify()
                return

    logger.info(
        "[%s] Microtask %s: DbC comments self-review complete (%d file(s) updated)",
        task_id,
        mt.id,
        len(dbc_files),
    )
