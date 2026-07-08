"""CodingTeamSwarm review mixin: Tech Lead review, merge, and revision/fail bookkeeping.

Extracted from ``coding_team/orchestrator.py`` (issue: decompose the orchestrator
god-file into named collaborators) — pure structural move, no behavior change.
Composed onto ``CodingTeamSwarm`` in orchestrator.py alongside the assignment and
implementation mixins.

A few names used here (``MAX_TASK_REVISIONS``, ``ActivityBridge``, ``_feature_branch_name``,
``_build_review_evidence``, ``_review_concurrency``) are defined in
``coding_team/orchestrator.py`` itself and referenced via a late-bound module reference
(``_orch.NAME``, resolved at call time) rather than imported by name at module load
time — see the equivalent note in ``coding_team/swarm_implementation.py`` for why
(circular import at load time, and monkeypatchability of ``MAX_TASK_REVISIONS``/
``ActivityBridge`` in tests).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from coding_team import hitl
from coding_team.models import Task, TaskStatus

logger = logging.getLogger(__name__)


class _ReviewMixin:
    """Tech Lead review, merge, and revision/fail bookkeeping for CodingTeamSwarm."""

    def _return_for_revision(self, task: Task, feedback: List[Dict[str, Any]]) -> bool:
        """Return a task to TODO for revision. Returns False (task not ready for review).

        Returns True only when the revision budget is exhausted (accept as-is). When a no-change
        loop is detected the task is escalated to the Tech Lead (terminal or a fresh window) and
        this returns False so the caller does not push the unchanged work into review.
        """
        from coding_team import orchestrator as _orch

        # Records the gate feedback and escalates on a no-change loop (so the Tech Lead adjudicates
        # over this round's reason); the feedback is now persisted for the status writes below.
        if self._escalate_if_no_change(task, feedback):
            return False
        revision_count = task.revision_count + 1
        if revision_count >= _orch.MAX_TASK_REVISIONS:
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
        # revision_feedback already carries this round's gate feedback (appended above, before the
        # no-change check); only the status/count change here, so do not re-append it.
        self.graph.update_task(
            task.id,
            status=TaskStatus.TO_DO,
            revision_count=revision_count,
        )
        # Release the task before the next round (status went to TO_DO above): it must be genuinely
        # unassigned and its agent freed, or it stays mapped to its agent and can be double-assigned.
        self.graph.unassign_task(task.id)
        return False

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
        Postconditions:
            - Returns human-readable lines (``"{question} → {answer}"``, or the bare answer for an
              answer-only record) deduplicated as described, in first-seen order (a superseding
              answer updates the existing line in place). Empty when no decision exists.
            - A ``user_decision`` entry predating the structured ``decisions`` field (one resumed
              across an upgrade) contributes its rendered ``reason`` bullets, so its decisions are
              still surfaced to the reviewer rather than dropped.
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

        def _add_legacy_reason(reason: str) -> None:
            # Pre-"decisions" entry: ``reason`` is a multi-line block (preamble + "- q → a" bullets).
            # Extract just the bullets so legacy decisions render as clean individual lines and
            # de-duplicate like structured ones; a bullet-less reason is surfaced whole.
            bullets = [
                ln.strip()[2:].strip()
                for ln in str(reason).splitlines()
                if ln.strip().startswith("- ")
            ]
            for line in bullets or [str(reason).strip()]:
                if line:
                    _put(hitl.normalize_key(line), line)

        for rec in self.resolved_questions or []:
            _add_record(rec)
        for entry in task.revision_feedback or []:
            if not (isinstance(entry, dict) and entry.get("source") == "user_decision"):
                continue
            # Gate on field presence, not truthiness: a new entry always carries "decisions" (an
            # empty list contributes nothing); only a legacy entry that predates the field falls
            # back to its rendered reason.
            if "decisions" in entry:
                for rec in entry.get("decisions") or []:
                    _add_record(rec)
            elif entry.get("reason"):
                _add_legacy_reason(str(entry["reason"]))
        return [line_by_key[key] for key in order]

    def _compute_review(
        self, task: Task, progress_callback: Any = None
    ) -> tuple[str, Dict[str, Any]]:
        """Collect the branch diff and run the Tech Lead review for one IN_REVIEW task.

        The read-only half of review: it computes the branch diff (git object-DB reads) and makes the
        review LLM call, mutating neither the working tree nor the task graph — so it is safe to run
        concurrently across tasks. The merge/revision decision is applied separately and serially by
        ``_apply_review_decision``; the caller owns any progress-bar lifecycle.

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
        """
        from coding_team import orchestrator as _orch

        try:
            from shared_git.git_utils import DEVELOPMENT_BRANCH, branch_diff

            branch = _orch._feature_branch_name(task)
            summary = task.changes_summary or "(no summary recorded)"
            diff = branch_diff(self.path, DEVELOPMENT_BRANCH, branch)
            evidence = _orch._build_review_evidence(summary, diff)
            review = self.tech_lead.run_code_review(
                task_title=task.title,
                task_description=task.description,
                acceptance_criteria=task.acceptance_criteria,
                changes_summary=evidence,
                user_decisions=self._user_decisions_for(task),
                progress_callback=progress_callback,
            )
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
            - ``error`` → task FAILED once (no revision loop); ``approved`` → branch merged and task
              MERGED; otherwise → task sent back to its engineer for revision. Exactly one of these.
        """
        if review.get("error"):
            # The review itself could not run (e.g. evidence exceeded the model context window). Do
            # NOT route this through the revision loop — re-sending the same failing prompt every
            # round would burn the whole revision budget at max cost. Fail the task once instead.
            self._fail_task(task, review, "Tech Lead review could not be completed")
        elif review.get("approved"):
            from coding_team import orchestrator as _orch
            from shared_git.git_utils import DEVELOPMENT_BRANCH, merge_branch

            try:
                ok, _ = merge_branch(
                    self.path, _orch._feature_branch_name(task), DEVELOPMENT_BRANCH
                )
                if ok:
                    self.graph.mark_branch_merged(task.id)
            except Exception as e:
                logger.warning("Merge failed for %s: %s; marking merged anyway", task.id, e)
                self.graph.mark_branch_merged(task.id)
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
        from coding_team import orchestrator as _orch

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
            from shared_concurrency import parallel_map

            update_fn(status_text=f"Tech Lead reviewing {len(in_review)} task(s)")
            results = parallel_map(
                in_review,
                self._compute_review,
                max_workers=_orch._review_concurrency(),
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
        from coding_team import orchestrator as _orch

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
            - task.status is IN_PROGRESS (revision pending) or FAILED (exhausted); never left
              IN_REVIEW with no state change, so the swarm loop cannot deadlock on it.
        """
        from coding_team import orchestrator as _orch

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
        revision_count = task.revision_count + 1
        if revision_count >= _orch.MAX_TASK_REVISIONS:
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
            return
        logger.info(
            "Task %s rejected by Tech Lead (revision %d); returning to engineer %s",
            task.id,
            revision_count,
            task.assigned_agent_id,
        )
        # Keep the assignment (do not clear assigned_agent_id / the agent->task mapping) so the
        # same engineer picks it up next round and revises the current work.
        self.graph.update_task(
            task.id,
            status=TaskStatus.IN_PROGRESS,
            revision_count=revision_count,
        )

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
