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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

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
        ``code`` is concatenated into the ``### path ###``-headered format
        ``DbcCommentsAgent``/``parse_code_into_file_blocks`` expect (one
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
    concatenated = "\n\n".join(f"### {path} ###\n{content}" for path, content in code.items())

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


def _revert_disk(prior_disk: Dict[Path, Optional[bytes]]) -> None:
    """Best-effort restore of each snapshotted path to its prior on-disk bytes.

    Postconditions:
        Each path with ``prior_bytes is None`` is deleted (it did not exist
        before); each other path is restored to ``prior_bytes``. An ordinary
        filesystem failure (e.g. the disk that caused the original write to
        fail is still unwritable) is logged and skipped per-path rather than
        raised -- this is already the fallback path of a "never fails"
        phase, so it must not itself introduce a new way to fail loud.
    """
    for full_path, prior_bytes in prior_disk.items():
        try:
            if prior_bytes is None:
                full_path.unlink(missing_ok=True)
            else:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_bytes(prior_bytes)
        except OSError as exc:
            logger.warning("Failed to revert %s to its prior state (ignored): %s", full_path, exc)


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
        ``repo_path``. If ``deps.build_verifier`` is set and reports failure
        for the post-insertion tree, ONLY the file(s) this phase's DbC result
        touched are reverted -- on disk (prior content restored, or the file
        deleted if it was newly created) and in ``microtask_files``/
        ``all_files``/``mt.output_files`` (prior value restored, or the key
        popped if it did not exist before); every other file is left
        untouched. A ``gate_config.run_dbc_self_review`` exception, an unsafe
        write path, an ordinary filesystem failure while snapshotting or
        writing (e.g. a full disk or revoked permissions), or a build-verifier
        exception are all logged and skipped/reverted rather than propagated
        -- this phase never raises or
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

    dbc_files: Dict[str, str] = getattr(dbc_result, "files", None) or {}
    if not dbc_files:
        logger.info("[%s] Microtask %s: DbC self-review made no changes", task_id, mt.id)
        return

    root = Path(repo_path).resolve()
    output_files = mt.output_files if isinstance(getattr(mt, "output_files", None), dict) else {}

    # Snapshot prior state for exactly the touched keys, before any write.
    # Deliberately not rollback.py's whole-microtask _MicrotaskRollback
    # machinery (its revert wholesale-clears mt.output_files) -- a
    # build-verifier failure here must revert only the DbC-touched file(s),
    # leaving the rest of the microtask's output untouched.
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
    except OSError as exc:
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
        try:
            ok, msg = deps.build_verifier(repo_path, build_verify_label, task_id)
        except Exception as exc:
            logger.warning("[%s] Microtask %s: DbC build verifier raised: %s", task_id, mt.id, exc)
            ok, msg = False, str(exc)

        if not ok:
            logger.warning(
                "[%s] Microtask %s: build failed after DbC comments (%s), reverting %d file(s)",
                task_id,
                mt.id,
                msg,
                len(dbc_files),
            )
            for rel_path in dbc_files:
                _restore_dict_entry(microtask_files, rel_path, prior_microtask[rel_path])
                _restore_dict_entry(all_files, rel_path, prior_all[rel_path])
                _restore_dict_entry(output_files, rel_path, prior_output[rel_path])
            mt.output_files = output_files
            _revert_disk(prior_disk)
            return

    logger.info(
        "[%s] Microtask %s: DbC comments self-review complete (%d file(s) updated)",
        task_id,
        mt.id,
        len(dbc_files),
    )
