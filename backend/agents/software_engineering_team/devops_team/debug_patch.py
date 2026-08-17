"""Phase 3 branch/write gate and Phase 4.6 debug-patch retry loop (devops_team).

Split out of ``orchestrator.py``. ``debug_patch_once`` takes the owning
``DevOpsTeamLeadAgent`` instance (duck-typed as ``agent``), following the
``tool_dispatch.py`` convention, because it is invoked repeatedly inside a
bounded retry loop (not once per pipeline run like Phase 1/2) and needs live
access to ``agent.infra_debug_agent``, ``agent.infra_patch_agent``,
``agent._run_execution_tools``, and ``agent._report_status``.
``run_phase3_branch_write`` takes its git branch callables as explicit
injected kwargs so ``orchestrator.py`` can pass through its own module
globals, keeping existing test monkeypatches on those names working
unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared.git.branch_utils import make_branch_suffix
from shared.git.git_utils import DEVELOPMENT_BRANCH
from software_engineering_team.shared.repo_writer import NO_FILES_TO_WRITE_MSG, write_agent_output

from .infra_debug_agent import IaCDebugInput
from .infra_patch_agent import IaCPatchInput
from .models import DevOpsTaskSpec, DevOpsTeamResult

logger = logging.getLogger(__name__)

# Bounded Phase 4.6 debug → patch → re-exec iterations for fixable infra failures.
MAX_INFRA_FIX_ITERATIONS = 3


@dataclass
class _DebugPatchState:
    """Mutable bag for one Phase 4.6 debug-patch retry session.

    Invariants: ``exec_failures`` is derived from ``exec_results`` (dict entries
    where ``success`` is falsy). ``exec_gate_map`` / ``exec_findings`` always
    mirror ``exec_results`` — established in ``__post_init__`` and refreshed
    via :meth:`refresh_aggregates` after each re-exec. Malformed entries (not a
    dict, or with non-dict ``checks`` / non-list ``findings``) are skipped
    rather than raising, since execution-tool output is untrusted external
    input; consumers of ``exec_failures`` (e.g. the Phase 4.6 debug-patch loop)
    call ``.get()`` on each entry without further isinstance checks, so
    non-dict entries must never reach that list.
    """

    exec_results: List[Dict[str, Any]]
    exec_gate_map: Dict[str, str] = field(init=False, default_factory=dict)
    exec_findings: List[str] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.refresh_aggregates()

    @property
    def exec_failures(self) -> List[Dict[str, Any]]:
        """Failing execution-tool results derived from ``exec_results``.

        Postconditions: only dict entries are included — a non-dict entry is
        logged and excluded rather than being surfaced as a "failure" that
        downstream ``.get()`` calls (e.g. in ``debug_patch_once``) cannot
        safely handle. A non-list ``findings`` value is normalized to ``[]``
        (on a shallow copy) so ``debug_patch_once``'s unguarded
        ``"\\n".join(ef.get("findings", []))`` — which only falls back to its
        default when the key is *absent*, not when it's present but the wrong
        type — never receives a non-iterable.
        """
        failures = []
        for er in self.exec_results:
            if not isinstance(er, dict):
                logger.warning("DevOps execution result is not a dict: %r", er)
                continue
            if not er.get("success", True):
                findings = er.get("findings")
                if not isinstance(findings, list):
                    if findings is not None:
                        logger.warning(
                            "DevOps execution result has non-list findings: %r", findings
                        )
                    er = {**er, "findings": []}
                failures.append(er)
        return failures

    def refresh_aggregates(self) -> None:
        """Rebuild ``exec_gate_map`` / ``exec_findings`` from ``exec_results``.

        Preconditions: ``exec_results`` is the latest execution-tool output list.
        Postconditions: ``exec_gate_map`` and ``exec_findings`` mirror that list;
          entries that aren't a dict, or whose ``checks``/``findings`` aren't
          the expected dict/list shape, are skipped and logged rather than
          raising.
        """
        self.exec_gate_map = {}
        self.exec_findings = []
        for er in self.exec_results:
            if not isinstance(er, dict):
                logger.warning("DevOps execution result is not a dict: %r", er)
                continue
            checks = er.get("checks")
            if isinstance(checks, dict):
                self.exec_gate_map.update(checks)
            findings = er.get("findings")
            if isinstance(findings, list):
                self.exec_findings.extend(findings)


def run_phase3_branch_write(
    *,
    write_changes: bool,
    aggregated_artifacts: Dict[str, str],
    repo_path: Path,
    task_spec: DevOpsTaskSpec,
    subdir: str,
    ensure_development_branch: Callable[[Path], Tuple[bool, str]],
    create_feature_branch: Callable[[Path, str, str], Tuple[bool, str]],
    report_status: Callable[..., None],
) -> Optional[DevOpsTeamResult]:
    """Phase 3: feature branch + artifact write gates.

    Preconditions: Phase 2 returned ``None`` (artifacts may be empty).
    Postconditions: when ``write_changes`` and artifacts are present, prepares
      the development branch, cuts a feature branch, and writes artifacts —
      returning a failed ``DevOpsTeamResult`` on any of those gates; otherwise
      reports phase-3 status and returns ``None``.
    """
    if write_changes and aggregated_artifacts:
        # Cut a feature branch from development up front (mirroring the
        # code-v2 teams) so every intermediate write/patch commit lands
        # on the branch and development stays clean until the reviewed
        # Phase 5 merge. Without this, writes would commit straight to
        # the checked-out development branch and the later merge would
        # be an empty no-op.
        dev_ok, dev_msg = ensure_development_branch(repo_path)
        if not dev_ok:
            return DevOpsTeamResult(
                success=False,
                failure_reason=(f"Cannot prepare {DEVELOPMENT_BRANCH} branch: {dev_msg}"),
            )
        branch_ok, branch_msg = create_feature_branch(
            repo_path,
            DEVELOPMENT_BRANCH,
            make_branch_suffix(task_spec.task_id, task_spec.title),
        )
        if not branch_ok:
            return DevOpsTeamResult(
                success=False,
                failure_reason=f"Cannot create feature branch: {branch_msg}",
            )
        ok, msg = write_agent_output(
            repo_path=repo_path,
            output={
                "files": aggregated_artifacts,
                "commit_message": (f"feat(devops): implement task [{task_spec.task_id}]"),
            },
            subdir=subdir,
        )
        if not ok and msg != NO_FILES_TO_WRITE_MSG:
            return DevOpsTeamResult(success=False, failure_reason=msg)

    report_status(
        "phase3",
        detail=(
            "DevOps team pipeline: phase 3 - branch + implementation "
            f"({len(aggregated_artifacts)} artifact files)"
        ),
    )
    return None


def debug_patch_once(
    agent: Any,
    fix_iter: int,
    *,
    state: _DebugPatchState,
    aggregated_artifacts: Dict[str, str],
    repo_path: Path,
    repo_str: str,
    write_changes: bool,
    subdir: str,
    max_iterations: int,
) -> Optional[_DebugPatchState]:
    """Run one infra debug → patch → re-exec iteration.

    Parameters:
      agent: owning ``DevOpsTeamLeadAgent`` instance (duck-typed) providing
        ``_report_status``, ``infra_debug_agent``, ``infra_patch_agent``, and
        ``_run_execution_tools``.
      fix_iter: 0-based iteration index (status logging).
      state: mutable debug-patch state bag; ``exec_failures`` drives the
        iteration.
      aggregated_artifacts: mutable artifact path → content map; updated
        in place when a patch is applied.
      repo_path: repository path on disk (for optional writes).
      repo_str: string form of ``repo_path`` passed to tool agents.
      write_changes: when True, persist patched files via
        ``write_agent_output`` before re-exec.
      subdir: subdirectory scope for ``write_agent_output``.
      max_iterations: bound shown in status logs (enforced by
        ``_run_bounded_retry_loop``, not re-asserted here).

    Preconditions:
      - ``fix_iter`` is a 0-based index from the bounded-retry helper
      - ``state.exec_failures`` is expected to be non-empty when invoked by
        the helper; if empty, returns ``state`` unchanged
    Postconditions:
      - Empty ``state.exec_failures`` → return ``state`` unchanged
      - Soft abort (debug/patch exception, not fixable, or empty patches)
        → log and return ``None`` (retry helper stops; no further attempts)
      - Failed patch write → log a warning, still re-exec against the
        in-memory (and possibly on-disk) patch, then return ``state`` so
        validation is not skipped after a persistence failure
      - Successful debug/patch/re-exec that resolves all execution failures
        → ``state.exec_failures`` is cleared and ``state`` is returned
      - Partial success (some failures remain after re-exec) → return
        ``state`` with updated ``exec_failures`` so the retry helper can
        continue to the next iteration
    """
    if not state.exec_failures:
        return state

    agent._report_status(
        "phase4.6",
        detail=(
            "DevOps team pipeline: phase 4.6 - debug-patch iteration "
            f"{fix_iter + 1}/{max_iterations} ({len(state.exec_failures)} failures)"
        ),
    )
    combined_output = "\n---\n".join(
        "\n".join(ef.get("findings", [])) for ef in state.exec_failures
    )
    first_tool = state.exec_failures[0].get("tool", "unknown")
    first_cmd = state.exec_failures[0].get("command", "unknown")
    try:
        debug_out = agent.infra_debug_agent.run(
            IaCDebugInput(
                execution_output=combined_output,
                tool_name=first_tool,
                command=first_cmd,
                artifacts=aggregated_artifacts,
            )
        )
    except Exception as dbg_err:
        logger.warning("DevOps debug agent failed: %s", dbg_err)
        return None
    if not debug_out.fixable:
        logger.info("DevOps debug agent: errors are not fixable via code changes")
        return None
    try:
        patch_out = agent.infra_patch_agent.run(
            IaCPatchInput(
                debug_output=debug_out,
                original_artifacts=aggregated_artifacts,
                repo_path=repo_str,
            )
        )
    except Exception as patch_err:
        logger.warning("DevOps patch agent failed: %s", patch_err)
        return None
    if not patch_out.patched_artifacts:
        logger.info("DevOps patch agent returned no patches")
        return None
    aggregated_artifacts.update(patch_out.patched_artifacts)
    if write_changes:
        ok, msg = write_agent_output(
            repo_path=repo_path,
            output={
                "files": patch_out.patched_artifacts,
                "commit_message": f"fix(devops): patch iteration {fix_iter + 1}",
            },
            subdir=subdir,
        )
        # Persistence failure must not skip re-exec: patched content is
        # already in ``aggregated_artifacts`` and may already be on disk
        # (e.g. commit-hook reject after write). Soft-aborting here would
        # let Phase 5 commit unvalidated patches.
        if not ok and msg != NO_FILES_TO_WRITE_MSG:
            logger.warning(
                "DevOps patch write failed (%s); continuing with re-exec validation",
                msg,
            )
    state.exec_results = agent._run_execution_tools(repo_str, aggregated_artifacts)
    state.refresh_aggregates()
    return state
