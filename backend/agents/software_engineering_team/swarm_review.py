"""CodingTeamSwarm review mixin: Tech Lead review, merge, and revision/fail bookkeeping.

Extracted from ``coding_team/orchestrator.py`` (issue: decompose the orchestrator
god-file into named collaborators) — pure structural move, no behavior change.
Composed onto ``CodingTeamSwarm`` in orchestrator.py alongside the assignment and
implementation mixins.

Names defined in ``orchestrator.py`` (``MAX_TASK_REVISIONS``, ``ActivityBridge``,
``_feature_branch_name``, ``_build_review_evidence``) are referenced via a late-bound
module reference (``_orch.NAME``, resolved at call time) rather than imported by
name at module load time — see the equivalent note in
``coding_team/swarm_implementation.py`` for why (circular import at load time, and
monkeypatchability of ``MAX_TASK_REVISIONS``/``ActivityBridge`` in tests).

``_review_concurrency`` lives in ``progress_config`` and is late-bound the same way.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from software_engineering_team import hitl
from software_engineering_team.models import Task, TaskStatus

logger = logging.getLogger(__name__)


def _review_verdict_cache_key(
    *,
    task_title: str,
    task_description: str,
    acceptance_criteria: List[str],
    evidence: str,
    user_decisions: List[str],
    spec_content: str,
) -> str:
    """Hash of every input that determines one task's Tech Lead review verdict.

    Postconditions:
        - Two calls collide only when every argument is identical — covering
          every field ``run_code_review`` is actually called with
          (``task_title``/``task_description``/``acceptance_criteria``/
          ``changes_summary``/``user_decisions``/``spec_content``), so a
          change to any one of them — not just the branch diff — misses the
          cache. ``evidence`` already embeds the branch diff verbatim whenever
          it is non-empty (see ``orchestrator._build_review_evidence``), so
          this key alone also captures every diff change — a separate
          branch-digest component is not needed.
        - Hashes a JSON serialization (``sort_keys=True``) rather than a flat
          separator-joined string — mirroring
          ``code_review_agent.mapping._stable_json_digest``'s "unambiguous
          structured hash" design (reimplemented locally rather than
          imported, since that helper is deliberately module-private to
          ``mapping.py``). A flat join of ``[..., *acceptance_criteria,
          evidence, *user_decisions, ...]`` cannot distinguish where one
          variable-length list ends and the next begins — e.g.
          ``acceptance_criteria=["a", "b"], evidence="c", user_decisions=[]``
          and ``acceptance_criteria=["a"], evidence="b", user_decisions=["c"]``
          would flatten to the same sequence — whereas JSON's array/object
          delimiters make every field's boundary explicit.
    """
    payload = {
        "task_title": task_title,
        "task_description": task_description,
        "acceptance_criteria": acceptance_criteria,
        "evidence": evidence,
        "user_decisions": user_decisions,
        "spec_content": spec_content,
    }
    body = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def serialize_review_cache(
    cache: Dict[str, "tuple[str, Dict[str, Any]]"],
) -> List[Dict[str, Any]]:
    """Convert the in-memory review verdict cache to a JSON-safe list.

    Preconditions:
        - ``cache`` maps task id -> (cache_key, verdict) tuples, matching
          ``CodingTeamSwarm._review_verdict_cache``'s declared type.

    Postconditions:
        - Returns one dict per kept entry: ``{"task_id", "cache_key", "verdict"}``.
        - At most 20 entries are returned; when ``cache`` has more than 20
          entries, only the last 20 in the dict's iteration (insertion)
          order are kept.
        - Empty cache returns ``[]``.
        - Result is pure JSON-safe (no tuples; the nested ``verdict`` dict
          is deep-copied so later mutation of the live cache cannot alias it).
    """
    max_cached_verdicts = 20
    items = list(cache.items())[-max_cached_verdicts:]
    return [
        {"task_id": task_id, "cache_key": cache_key, "verdict": copy.deepcopy(verdict)}
        for task_id, (cache_key, verdict) in items
    ]


def deserialize_review_cache(data: Any) -> Dict[str, "tuple[str, Dict[str, Any]]"]:
    """Restore the in-memory review verdict cache from serialize_review_cache's output.

    Preconditions:
        - None — ``data`` may be any value, including corrupt/malformed
          stored state; this function never raises on bad input.

    Postconditions:
        - Non-list ``data`` (None, str, int, dict, ...) returns ``{}``.
        - Each list entry is included only when it is a dict containing all
          of ``task_id``, ``cache_key``, ``verdict``; ``task_id`` and
          ``cache_key`` are both ``str``; and ``verdict`` is a dict with a
          real ``bool`` ``approved`` field — entries failing any check are
          skipped. The ``task_id``/``cache_key`` type check also guards the
          dict-key assignment below against an unhashable ``task_id`` (e.g.
          a list or dict smuggled into corrupted JSON), which would
          otherwise raise ``TypeError`` instead of being skipped. The
          ``approved`` check matters beyond crash-safety: ``_compute_review``
          only ever caches a non-``error`` ``run_code_review`` verdict, which
          always carries a real bool ``approved``, so this rejects exactly
          the corrupted shapes that could otherwise be misread as a genuine
          approve/reject decision (e.g. ``_apply_review_decision`` treats
          any truthy ``review.get("approved")`` as approval, so a corrupted
          non-bool like ``"false"`` would wrongly merge the task) while
          never rejecting a verdict this module actually wrote. A truthy
          ``verdict["error"]`` is rejected for the same reason: an error
          verdict is never cached live (``_compute_review`` excludes it), so
          a restored one is definitionally corrupted, and ``_apply_review_decision``
          would otherwise route it through its error-first branch and fail
          the task instead of rerunning the review. ``reason`` (if present)
          must be a ``str`` and ``requested_changes`` (if present) must be a
          ``list`` — ``_request_revision`` feeds both straight into revision
          feedback shown to the implementer (``review.get("reason", "")`` /
          ``review.get("requested_changes") or []``), so a malformed value
          would otherwise be threaded through as if it were real reviewer
          feedback; a verdict this module wrote always has both fields in
          this shape (or omits them, which the same ``.get`` defaults cover).
        - Returns a dict keyed by ``task_id`` with ``(cache_key, verdict)``
          tuple values, mirroring
          ``CodingTeamSwarm._review_verdict_cache``'s declared type.
        - When multiple kept entries share a ``task_id``, the later entry
          (by list order) wins — matching plain dict-construction semantics
          and ``serialize_review_cache``'s insertion-order-preserving output,
          so ``deserialize_review_cache(serialize_review_cache(cache)) ==
          cache`` for any cache with at most 20 entries (``verdict`` dicts
          are not deep-copied here since the caller owns the freshly parsed
          ``data``).
        - At most 20 entries are ever returned, mirroring
          ``serialize_review_cache``'s cap: when more than 20 entries in
          ``data`` pass validation, only the last 20 (by list order) are
          kept. This holds even for a stored value that itself has more
          than 20 entries (e.g. hand-edited or written by something other
          than ``serialize_review_cache``) — restore never grows the live
          cache past the size the rest of the system assumes it is bounded
          to.
    """
    if not isinstance(data, list):
        return {}
    max_cached_verdicts = 20
    result: Dict[str, "tuple[str, Dict[str, Any]]"] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if not {"task_id", "cache_key", "verdict"} <= entry.keys():
            continue
        task_id = entry["task_id"]
        cache_key = entry["cache_key"]
        verdict = entry["verdict"]
        if (
            not isinstance(task_id, str)
            or not isinstance(cache_key, str)
            or not isinstance(verdict, dict)
        ):
            continue
        if not isinstance(verdict.get("approved"), bool):
            continue
        if verdict.get("error"):
            continue
        if "reason" in verdict and not isinstance(verdict["reason"], str):
            continue
        if "requested_changes" in verdict and not isinstance(verdict["requested_changes"], list):
            continue
        result[task_id] = (cache_key, verdict)
    if len(result) > max_cached_verdicts:
        result = dict(list(result.items())[-max_cached_verdicts:])
    return result


class _ReviewMixin:
    """Tech Lead review, merge, and revision/fail bookkeeping for CodingTeamSwarm."""

    def _return_for_revision(self, task: Task, feedback: List[Dict[str, Any]]) -> bool:
        """Return a task to TODO for revision. Returns False (task not ready for review).

        Returns True only when the revision budget is exhausted (accept as-is). When a no-change
        loop is detected the task is escalated to the Tech Lead (terminal or a fresh window) and
        this returns False so the caller does not push the unchanged work into review.
        """
        # Records the gate feedback and escalates on a no-change loop (so the Tech Lead adjudicates
        # over this round's reason); the feedback is now persisted for the status writes below.
        if self._escalate_if_no_change(task, feedback):
            return False

        def _accept_as_is(revision_count: int) -> bool:
            from software_engineering_team import coding_team_orchestrator as _orch

            logger.warning(
                "Task %s exceeded max revisions (%d); accepting as-is",
                task.id,
                _orch.MAX_TASK_REVISIONS,
            )
            # Persist the final revision_count even when accepting as-is, so the task's recorded
            # count is accurate and consistent with the FAILED/IN_PROGRESS paths (which all persist
            # the bump). Otherwise a later bounce would re-derive the count from a stale value.
            self.graph.update_task(task.id, revision_count=revision_count)
            return True  # accept despite issues

        def _bounce(revision_count: int) -> bool:
            # revision_feedback already carries this round's gate feedback (appended above, before
            # the no-change check); only the status/count change here, so do not re-append it.
            self.graph.update_task(
                task.id,
                status=TaskStatus.TO_DO,
                revision_count=revision_count,
            )
            # Release the task before the next round (status went to TO_DO above): it must be
            # genuinely unassigned and its agent freed, or it stays mapped to its agent and can be
            # double-assigned.
            self.graph.unassign_task(task.id)
            return False

        return self._bump_and_check_revision_cap(
            task, on_exhausted=_accept_as_is, on_continue=_bounce
        )

    def _user_decisions_for(self, task: Task) -> List[str]:
        """Render the user's already-made decisions for ``task`` as 'question → answer' lines.

        Combines plan-level decisions (``self.resolved_questions`` — answered at the entry gate or
        during Tech Lead planning) with task-level decisions the engineer escalated mid-implementation
        (the structured ``decisions`` recorded on each ``user_decision`` revision-feedback entry).
        Both review gates pass the result to their reviewer so a settled question is never re-raised.

        De-duplication is by normalized question text, last-answer-wins: a later answer to the same
        question (a task-level escalation) supersedes an earlier one (a plan-level answer), so the
        reviewer is never shown two conflicting answers to the same question. Records with no question
        text (answer-only) are keyed by their full rendered line instead, so they de-duplicate against
        identical lines but are never dropped.

        Preconditions:
            - Entries in ``self.resolved_questions`` and ``task.revision_feedback`` are dicts;
              non-dict entries and records with no renderable content are skipped.
            - Every ``user_decision`` entry in ``task.revision_feedback`` carries a structured
              ``decisions`` field (possibly empty); an entry predating that field (a pre-upgrade
              resume) violates this and is a fail-fast error, not a supported input shape.
        Postconditions:
            - Returns human-readable lines (``"{question} → {answer}"``, or the bare answer for an
              answer-only record) deduplicated as described, in first-seen order (a superseding
              answer updates the existing line in place). Empty when no decision exists.
            - Raises ``ValueError`` if a ``user_decision`` entry lacks a ``decisions`` field —
              resume of that legacy shape is unsupported; the caller (``_compute_review``) turns
              this into a clean per-task review failure rather than silently parsing ``reason``.
        """
        order: List[str] = []
        line_by_key: Dict[str, str] = {}

        def _put(key: str, line: str) -> None:
            # Last-wins: a later answer to the same key replaces the earlier line but keeps its
            # first-seen position, so a task-level escalation overrides the plan-level answer.
            if key not in line_by_key:
                order.append(key)
            line_by_key[key] = line

        def _add_record(rec: Any) -> None:
            if not isinstance(rec, dict):
                return
            line = hitl.render_decision_line(rec)
            if not line:
                return
            question, _answer = hitl.decision_qa(rec)
            _put(hitl.normalize_key(question) if question else hitl.normalize_key(line), line)

        for rec in self.resolved_questions or []:
            _add_record(rec)
        for entry in task.revision_feedback or []:
            if not (isinstance(entry, dict) and entry.get("source") == "user_decision"):
                continue
            # Gate on field presence, not truthiness: every current entry carries "decisions" (an
            # empty list contributes nothing); an entry missing the field entirely predates it and
            # is an unsupported resume shape — fail fast rather than guess at its content.
            if "decisions" not in entry:
                raise ValueError(
                    f"Task {task.id}: user_decision revision_feedback entry has no structured "
                    "'decisions' field (pre-decisions legacy shape); resume is not supported for "
                    "this entry shape."
                )
            for rec in entry.get("decisions") or []:
                _add_record(rec)
        return [line_by_key[key] for key in order]

    def _compute_review(
        self, task: Task, progress_callback: Any = None
    ) -> tuple[str, Dict[str, Any]]:
        """Collect the branch diff and run the Tech Lead review for one IN_REVIEW task.

        The read-only half of review: it computes the branch diff (git object-DB reads) and makes the
        review LLM call, mutating neither the working tree nor the task graph — so it is safe to run
        concurrently across tasks. The merge/revision decision is applied separately and serially by
        ``_apply_review_decision``; the caller owns any progress-bar lifecycle.

        A task whose review inputs are byte-identical to its last-cached call (``_review_verdict_cache``,
        keyed by ``_review_verdict_cache_key`` over every field ``run_code_review`` actually sees —
        title/description/acceptance criteria, ``changes_summary``/``user_decisions``, and
        ``spec_content`` — not the branch diff alone) reuses that verdict instead of calling
        ``run_code_review`` again — the complementary, per-task counterpart to ``AgentReviewCache``'s
        per-file cache in the code-v2 execution loop. This only removes the redundant LLM call: the
        no-change bookkeeping downstream (``_escalate_if_no_change``/``_note_revision_progress``, driven
        by the separate, diff-only ``_branch_digest``) still sees the same rejected verdict every round
        on an unchanged branch and escalates to Tech Lead adjudication at the same cap as before caching.

        Preconditions:
            - ``task`` is IN_REVIEW with a recorded feature branch (or the default ``feature/{id}``).
            - ``progress_callback`` is None (the concurrent fan-out, which suppresses per-task
              progress so concurrent bridges don't race the one sub-progress slot) or a
              ``(step, detail, fraction)`` sink for the sole-review live bar.
        Postconditions:
            - Returns ``(diff, review)`` where ``review`` has the ``run_code_review`` shape
              (``approved``/``error``/``reason``/``requested_changes``). Any diff-prep or review
              exception is contained and converted into an ``error=True`` review (with an empty diff),
              so one task's failure fails only that task once (via ``_apply_review_decision``) and
              never aborts the round; no graph or git state is changed here.
            - A cache hit returns an independent deep copy of the stored verdict, never the shared
              instance, so the caller may mutate it freely. A fresh, non-``error`` verdict is stored
              under ``task.id`` keyed by this round's cache key, overwriting any prior entry for that
              task — an ``error`` verdict is never cached (mirrors
              ``code_review_agent.mapping``'s chunk cache: a review that could not run must be
              retried for real, not frozen).
        """
        from software_engineering_team import coding_team_orchestrator as _orch

        try:
            from shared.git.git_utils import DEVELOPMENT_BRANCH, branch_diff

            branch = _orch._feature_branch_name(task)
            summary = task.changes_summary or "(no summary recorded)"
            diff = branch_diff(self.path, DEVELOPMENT_BRANCH, branch)
            # Computed unconditionally (cheap: pure string/list ops, no I/O) because both feed the
            # cache key below — a byte-identical branch with a changed changes_summary (every
            # run_implement call overwrites it, whether or not the diff moved) or a newly answered
            # HITL decision (_escalate_decision appends to revision_feedback without necessarily
            # changing the branch) must still miss the cache, since either can change what the
            # reviewer is shown and thus the verdict.
            evidence = _orch._build_review_evidence(summary, diff)
            user_decisions = self._user_decisions_for(task)
            cache_key = _review_verdict_cache_key(
                task_title=task.title,
                task_description=task.description,
                acceptance_criteria=task.acceptance_criteria,
                evidence=evidence,
                user_decisions=user_decisions,
                spec_content=self.spec_content,
            )
            with self._review_verdict_cache_lock:
                cached = self._review_verdict_cache.get(task.id)
            if cached is not None and cached[0] == cache_key:
                logger.info(
                    "Task %s: review inputs unchanged since last review; reusing cached verdict",
                    task.id,
                )
                return diff, copy.deepcopy(cached[1])

            review = self.tech_lead.run_code_review(
                task_title=task.title,
                task_description=task.description,
                acceptance_criteria=task.acceptance_criteria,
                changes_summary=evidence,
                user_decisions=user_decisions,
                progress_callback=progress_callback,
                spec_content=self.spec_content,
            )
            if not review.get("error"):
                with self._review_verdict_cache_lock:
                    self._review_verdict_cache[task.id] = (cache_key, copy.deepcopy(review))
            return diff, review
        except Exception as e:  # noqa: BLE001 — a failed review must never abort the swarm
            logger.warning("Tech Lead review preparation failed for %s: %s", task.id, e)
            return "", {
                "approved": False,
                "error": True,
                "reason": f"Review could not be prepared: {e}",
                "requested_changes": [],
            }

    def _apply_review_decision(self, task: Task, diff: str, review: Dict[str, Any]) -> None:
        """Apply one precomputed review verdict: fail, merge, or send back for revision.

        This is the serial half of review — it performs the git merge and task-graph mutations, so it
        must run one task at a time (the caller invokes it in the original IN_REVIEW order to keep
        merge ordering deterministic).

        Preconditions:
            - ``review`` is the value ``_compute_review`` returned for ``task``; ``diff`` is the diff
              it collected (empty string on an error review).
        Postconditions:
            - ``error`` → task FAILED once (no revision loop); ``approved`` and merge succeeds →
              branch merged and task MERGED; ``approved`` but merge fails without raising → task
              sent back for revision (same as an unapproved review, via ``_request_revision``) with
              the merge failure recorded as feedback; ``approved`` but ``merge_branch`` raises →
              task marked MERGED anyway (best-effort, with a warning logged, so an unexpected git
              failure doesn't crash the swarm); any other unapproved review → task sent back for
              revision. Exactly one of these.
        """
        if review.get("error"):
            # The review itself could not run (e.g. evidence exceeded the model context window). Do
            # NOT route this through the revision loop — re-sending the same failing prompt every
            # round would burn the whole revision budget at max cost. Fail the task once instead.
            self._fail_task(task, review, "Tech Lead review could not be completed")
        elif review.get("approved"):
            from shared.git.git_utils import DEVELOPMENT_BRANCH, merge_branch
            from software_engineering_team import coding_team_orchestrator as _orch

            try:
                ok, merge_msg = merge_branch(
                    self.path, _orch._feature_branch_name(task), DEVELOPMENT_BRANCH
                )
            except Exception as e:
                logger.warning("Merge failed for %s: %s; marking merged anyway", task.id, e)
                self.graph.mark_branch_merged(task.id)
                return
            if ok:
                self.graph.mark_branch_merged(task.id)
                return
            # Non-exception merge failure (e.g. conflict): do NOT leave the task silently stuck
            # IN_REVIEW — route it through the same revision-cap-bounded bounce every other
            # stuck-review path uses, with the merge failure recorded as feedback.
            logger.warning(
                "Merge rejected for %s: %s; sending back for revision", task.id, merge_msg
            )
            self._request_revision(
                task,
                {
                    "reason": f"Approved branch failed to merge: {merge_msg}",
                    "requested_changes": [],
                },
                diff=diff,
            )
        else:
            # Pass the diff already collected for the reviewer so the no-change check reuses it
            # rather than re-shelling out to git for the same branch.
            self._request_revision(task, review, diff=diff)

    def _review_and_merge(self, update_fn: Any) -> None:
        """Coordinator reviews completed tasks: merge approved ones, send rejected ones back.

        Reviews are independent (a read-only branch diff plus an LLM call), so a round with several
        tasks in review fans the reviews out concurrently (via ``parallel_map``, which propagates the
        caller's LLM-attribution contextvars into each worker) and then applies the merge/revision
        decisions serially in the original order. This keeps every git write and graph mutation
        single-threaded (deterministic merge ordering, branch isolation preserved) while collapsing k
        serial review latencies into roughly one.

        Preconditions:
            - ``update_fn`` is the job progress callback (or a no-op); it is only invoked from this
              (main) thread, never from the review workers.
        Postconditions:
            - Every task that was IN_REVIEW is left MERGED, IN_PROGRESS (revision pending), or FAILED
              — never IN_REVIEW with no state change — with the same verdict the prior serial loop
              produced. Collecting all diffs up front (before any merge) is behavior-preserving: the
              reviewer's evidence is ``branch_diff``'s three-dot ``base...branch`` diff, anchored on
              the branch's own divergence point, so an earlier same-round merge advancing the
              development tip does not change any other branch's computed diff.
        """
        from software_engineering_team import progress_config as _pc

        in_review = [t for t in self.graph.get_tasks() if t.status == TaskStatus.IN_REVIEW]
        if not in_review:
            return

        # Collect every task's (diff, review) first, then apply all decisions through one serial loop
        # (git writes + graph mutations stay single-threaded, in original order). A sole review runs
        # inline with its live per-task progress bar; two or more fan out via parallel_map, which
        # suppresses that bar (concurrent bridges would race the one sub-progress slot) but copies each
        # worker's LLM-attribution contextvars and preserves input order. _compute_review contains its
        # own exceptions, so no worker raises out of the pool.
        if len(in_review) == 1:
            results = [self._review_with_live_progress(in_review[0], update_fn)]
        else:
            from shared.concurrency import parallel_map

            update_fn(status_text=f"Tech Lead reviewing {len(in_review)} task(s)")
            results = parallel_map(
                in_review,
                self._compute_review,
                max_workers=_pc._review_concurrency(),
                skip_none=False,
            )

        for task, (diff, review) in zip(in_review, results):
            self._apply_review_decision(task, diff, review)

    def _review_with_live_progress(self, task: Task, update_fn: Any) -> tuple[str, Dict[str, Any]]:
        """Review one task while streaming the Tech Lead's attempt/retry reports to the job record.

        Used for the sole-review case, where a live per-task sub-progress bar is safe (no concurrent
        writers). Bridging the reports keeps silent LLM retries from looking like a hang.

        Postconditions:
            - Returns ``_compute_review(task, ...)``; the progress activity is cleared on every exit
              path (success or failure) so a stale sub-progress bar never lingers into the next round.
        """
        from software_engineering_team import coding_team_orchestrator as _orch

        tl_bridge = _orch.ActivityBridge(
            update_fn,
            agent="tech_lead_review",
            label="Tech Lead reviewing",
            task_id=task.id,
            task_title=task.title,
        )
        try:
            tl_bridge("preparing", "collecting branch diff", 0.0)
            return self._compute_review(task, tl_bridge)
        finally:
            tl_bridge.clear()

    def _request_revision(
        self, task: Task, review: Dict[str, Any], diff: Optional[str] = None
    ) -> None:
        """Send a Tech-Lead-rejected task back to the same implementation worker for revision.

        Unlike the quality-gate path (_return_for_revision, which demotes to TO_DO and clears the
        assignment), a Tech Lead rejection keeps the task with its current worker: status goes
        back to IN_PROGRESS so the same worker re-runs run_implement next round with the reviewer's
        reasons threaded into the prompt. On exhausting MAX_TASK_REVISIONS the task is marked
        FAILED (terminal) rather than merging code the Tech Lead rejected.

        Preconditions:
            - task is currently IN_REVIEW and assigned to a worker.
        Postconditions:
            - task.status is IN_PROGRESS (revision pending) or FAILED (exhausted); if the
              no-change cap is reached first, control passes to ``_escalate_to_tech_lead``
              instead, which may also leave the task MERGED (a "done" verdict) — see that
              method's Postconditions. Never left IN_REVIEW with no state change, so the swarm
              loop cannot deadlock on it.
        """
        entry = {
            "source": "tech_lead",
            "reason": review.get("reason", ""),
            "requested_changes": review.get("requested_changes") or [],
        }
        # Records this round's rejection and escalates on a no-change loop (so the Tech Lead
        # adjudicates over the full history, including why it just bounced the task); passes the
        # reviewer's already-computed diff so the no-change check does not re-shell out to git. The
        # feedback is now persisted for the status writes below.
        if self._escalate_if_no_change(task, [entry], diff=diff):
            return

        def _fail(revision_count: int) -> None:
            from software_engineering_team import coding_team_orchestrator as _orch

            logger.warning(
                "Task %s exceeded max revisions (%d) on Tech Lead review; marking FAILED. Reason: %s",
                task.id,
                _orch.MAX_TASK_REVISIONS,
                entry["reason"],
            )
            self.graph.update_task(
                task.id,
                status=TaskStatus.FAILED,
                revision_count=revision_count,
            )
            self._cascade_fail_dependents(task.id)

        def _continue(revision_count: int) -> None:
            logger.info(
                "Task %s rejected by Tech Lead (revision %d); returning to engineer %s",
                task.id,
                revision_count,
                task.assigned_agent_id,
            )
            # Keep the assignment (do not clear assigned_agent_id / the agent->task mapping) so
            # the same engineer picks it up next round and revises the current work.
            self.graph.update_task(
                task.id,
                status=TaskStatus.IN_PROGRESS,
                revision_count=revision_count,
            )

        self._bump_and_check_revision_cap(task, on_exhausted=_fail, on_continue=_continue)

    def _fail_task(self, task: Task, review: Dict[str, Any], context: str) -> None:
        """Terminally fail a task (and its dependents) without spinning the revision loop.

        Used when a Tech Lead review cannot be performed (e.g. the review evidence exceeded the
        model context window). Re-routing such a task through the revision loop would re-send the
        same failing prompt every round up to MAX_TASK_REVISIONS at max cost; instead we record
        the diagnostic, mark the task FAILED, and cascade the failure to dependents.

        Postconditions:
            - task.status is FAILED; tasks transitively depending on it are FAILED too.
        """
        entry = {
            "source": "tech_lead",
            "reason": review.get("reason", context),
            "requested_changes": [],
        }
        feedback = list(task.revision_feedback or []) + [entry]
        logger.warning(
            "%s for task %s; marking FAILED. Reason: %s", context, task.id, entry["reason"]
        )
        self.graph.update_task(task.id, status=TaskStatus.FAILED, revision_feedback=feedback)
        self._cascade_fail_dependents(task.id)

    def _cascade_fail_dependents(self, task_id: str) -> None:
        """Propagate a task's FAILED state to every task that can no longer be satisfied.

        A task depending on a FAILED task can never satisfy `_dependencies_satisfied` (which
        requires MERGED deps), so without this it would sit TO_DO forever and keep the swarm loop
        from completing. Delegates to `TaskGraphService.mark_dependents_failed`.
        """
        blocked = self.graph.mark_dependents_failed(task_id)
        if blocked:
            logger.warning("Task %s failure cascaded FAILED to dependents: %s", task_id, blocked)
