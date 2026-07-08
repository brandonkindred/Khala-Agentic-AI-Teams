"""CodingTeamSwarm implementation mixin: worker implementation, quality gates, and
no-change/Tech-Lead escalation.

Extracted from ``coding_team/orchestrator.py`` (issue: decompose the orchestrator
god-file into named collaborators) — pure structural move, no behavior change.
Composed onto ``CodingTeamSwarm`` in orchestrator.py alongside the assignment and
review mixins.

A few names used here (``MAX_TASK_REVISIONS``, ``ActivityBridge``, ``_NoopBridge``,
``_no_change_revisit_cap``, ``_feature_branch_name``) are defined in
``coding_team/orchestrator.py`` itself. They are referenced via a late-bound module
reference (``_orch.NAME``, resolved at call time) rather than imported by name at
module load time, for two reasons: (1) a module-level ``from coding_team.orchestrator
import NAME`` would be a circular import (orchestrator.py imports this module before
those names are defined in its own namespace), and (2) several of them
(``MAX_TASK_REVISIONS``, ``ActivityBridge``) are monkeypatched on
``coding_team.orchestrator`` in tests — a name copied at import time would not observe
that patch, while a late-bound module attribute lookup does.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from coding_team.models import Task, TaskStatus
from coding_team.pause_cycle import _format_decisions
from coding_team.team_routing import _quality_gate_agent_type

logger = logging.getLogger(__name__)


class _ImplementationMixin:
    """Worker implementation, quality gates, and no-change/Tech-Lead escalation."""

    def _implement_and_verify(
        self, swe: Any, update_fn: Any, *, live_progress: bool = True
    ) -> None:
        """Worker implements its assigned task in its own git worktree, then runs quality gates.

        Preconditions:
            - ``self._worktrees.prepare()`` has already completed — ``run()`` calls it once, up
              front, before any worker runs this round — so ``swe.agent_id`` has a prepared
              worktree.
            - ``live_progress=False`` when this call is part of a concurrent round fan-out (direct
              ``update_fn`` status-text calls and the code-review sub-progress bridge are both
              suppressed — concurrent live progress would race the one sub-progress slot, mirroring
              ``_review_and_merge``'s fan-out suppression); ``True`` (default) for the serial/solo
              path, which keeps today's live per-phase status text unchanged.
        Postconditions:
            - Never raises: any exception — including a failure to resolve this worker's worktree,
              or from ``run_implement`` itself — is contained and routed through
              ``_handle_incomplete_implementation`` exactly like a ``status="failed"`` result, so
              one worker's crash fails only its own task and never aborts the round. This is
              required, not defensive: ``shared_concurrency.parallel_map`` re-raises a worker
              exception to its caller and cancels the round's other pending tasks, so without this
              containment one worker crashing would abort every other concurrently-running
              worker's round too — worse than the prior serial loop, where a crash only prevented
              workers later in the loop from running this round.
        """
        task = self.graph.get_task_for_agent(swe.agent_id)
        if not task:
            return
        # Only (re)implement a task that is actively assigned for work. An IN_REVIEW task is awaiting
        # Tech Lead review — re-running the engineer on it would regenerate code already under review
        # and churn the loop. With un-assignment fixed this is belt-and-suspenders, but it makes the
        # worker's contract explicit and robust against any upstream assignment slip.
        if task.status != TaskStatus.IN_PROGRESS:
            return

        try:
            worktree_path = self._worktrees.path_for(swe.agent_id)
            if live_progress:
                update_fn(status_text=f"Implementing: {task.title}")
            result = swe.run_implement(task, worktree_path, repo_context=self.repo_context)

            if result.get("status") == "needs_decision":
                # The engineer hit a product/design decision it must not make. Escalate to the
                # user (never decide it here); thread the answer back so the next round
                # implements it.
                self._escalate_decision(task, result, update_fn)
                return

            if result.get("status") == "in_review":
                # Record the branch/summary BEFORE the gates so a gate-triggered revision (and its
                # no-change check) reads the branch the engineer actually used, not a stale/absent
                # one. feature_branch_agent_id pins this task to swe on any later reassignment
                # (see _assign_tasks) — the branch only exists checked out in swe's own worktree,
                # and git refuses to check it out (or delete/recreate it) from any other worktree
                # while it stays attached there.
                self.graph.update_task(
                    task.id,
                    feature_branch=result.get("feature_branch"),
                    feature_branch_agent_id=swe.agent_id,
                    changes_summary=result.get("changes_summary"),
                )
                # Run quality gates as tools, against this worker's own worktree.
                if not self._run_quality_gates(
                    swe,
                    task,
                    update_fn,
                    worktree_path=worktree_path,
                    live_progress=live_progress,
                ):
                    return  # task returned to TODO for revision (or escalated to the Tech Lead)
                self.graph.set_task_in_review(task.id)
            else:
                # Any non-review outcome — status="failed" (the LLM call raised) or
                # status="in_progress" (the model set ready_for_review=false / asked for another
                # pass) or any unexpected status — must be bounded. Otherwise the task stays
                # IN_PROGRESS and assigned, its revision_count never advances, and the same full
                # implement call repeats every round to the round cap, after which the task is
                # neither MERGED nor FAILED and the job is reported a clean success despite
                # incomplete work.
                logger.warning(
                    "Worker %s task %s did not reach review (status=%s): %s",
                    swe.agent_id,
                    task.id,
                    result.get("status"),
                    result.get("error"),
                )
                self._handle_incomplete_implementation(task, result)
        except Exception as exc:  # noqa: BLE001 - one worker's crash must fail only its own task
            logger.exception("Worker %s implementation raised for task %s", swe.agent_id, task.id)
            self._handle_incomplete_implementation(task, {"status": "failed", "error": str(exc)})

    def _branch_digest(self, task: Task, diff: Optional[str] = None) -> str:
        """Hash of the task's branch diff — the truthful 'did this round change anything' signal.

        ``branch_diff`` normalizes to "" for both an empty diff and a non-git/failed path, so a task
        that produced no change (or has no branch yet) hashes to a stable value and a repeat round
        compares equal. Pass ``diff`` to reuse a diff the caller already computed (the review path
        collects it for the reviewer) instead of paying for a second git invocation.

        Preconditions:
            - ``task`` is a task tracked by this swarm's graph; ``self.path`` is the repo checkout.
              ``diff`` is None (compute from the branch) or a diff string to hash directly.
        Postconditions:
            - Returns a hex digest; identical change states across rounds yield identical digests.
        """
        import hashlib

        from coding_team import orchestrator as _orch

        if diff is None:
            from shared_git.git_utils import DEVELOPMENT_BRANCH, branch_diff

            branch = _orch._feature_branch_name(task)
            diff = branch_diff(self.path, DEVELOPMENT_BRANCH, branch)
        return hashlib.sha256((diff or "").encode("utf-8", "replace")).hexdigest()

    def _note_revision_progress(self, task: Task, diff: Optional[str] = None) -> bool:
        """Record whether this revision round changed the code and report if the no-change cap is hit.

        Compares the task's current branch-diff digest to the digest recorded at the previous bounce:
        an identical digest means the engineer revisited the task without changing anything (a
        no-progress re-evaluation) and bumps ``no_change_revisits``; a different digest means real
        progress and resets the counter to 0. The first bounce only records a baseline.

        Preconditions:
            - ``task`` is a task tracked by this swarm's graph and is being bounced for revision this
              round; the caller has already appended this round's feedback to ``task``.
        Postconditions:
            - ``task.last_change_digest`` reflects the current change state and ``no_change_revisits``
              is incremented (no change) or reset to 0 (change), persisted via the graph.
            - Returns True iff ``no_change_revisits`` has reached the configured no-change cap, i.e.
              the caller should escalate to the Tech Lead instead of bouncing the task again.
        """
        from coding_team import orchestrator as _orch

        digest = self._branch_digest(task, diff=diff)
        if task.last_change_digest and task.last_change_digest == digest:
            no_change = task.no_change_revisits + 1
        else:
            no_change = 0
        self.graph.update_task(task.id, no_change_revisits=no_change, last_change_digest=digest)
        return no_change >= _orch._no_change_revisit_cap()

    def _escalate_to_tech_lead(self, task: Task) -> None:
        """Hand a task stuck in a no-change loop to the Tech Lead for direction; apply the verdict.

        Invoked when ``_note_revision_progress`` reports the no-change cap is reached: rather than
        bounce the same unchanged work again, give the Tech Lead the accumulated revision history
        (the documentation of what has been tried) and act on the verdict — close it out as already
        done, fail it terminally, or grant one more bounded window.

        Preconditions:
            - ``task`` is a non-terminal task tracked by this swarm's graph that has hit the
              no-change cap (``_note_revision_progress`` returned True for it).
        Postconditions:
            - "done": task is MERGED with ``resolved_without_changes=True`` (terminal, agent freed,
              dependents unblocked) and the reasoning recorded.
            - "fail": task is FAILED and its dependents cascade-failed.
            - "continue": ``no_change_revisits`` is reset to 0 (a fresh window) and the task returns
              to its engineer IN_PROGRESS; the 20-revision cap still ultimately bounds it.
        """
        from coding_team import orchestrator as _orch

        verdict_data = self.tech_lead.run_revision_adjudication(
            task_title=task.title,
            task_description=task.description,
            acceptance_criteria=task.acceptance_criteria,
            changes_summary=task.changes_summary or "",
            revision_feedback=task.revision_feedback or [],
        )
        verdict = verdict_data.get("verdict", "fail")
        reason = verdict_data.get("reason", "")
        entry = {
            "source": "tech_lead_adjudication",
            "reason": f"[{verdict}] {reason}".strip(),
            "requested_changes": [],
        }
        feedback = list(task.revision_feedback or []) + [entry]
        logger.info(
            "Task %s stalled (no change for %d round(s)); Tech Lead verdict=%s: %s",
            task.id,
            task.no_change_revisits,
            verdict,
            reason,
        )
        if verdict == "done":
            # "Done" means the task's goal is achieved — but the no-change cap only proves the branch
            # stopped changing, NOT that it is empty. A stalled-but-non-empty branch carries real,
            # unmerged work, so merge it (exactly like an approved review) before terminating;
            # otherwise mark_branch_merged would flip the graph to MERGED while the code never reaches
            # ``development`` and is silently dropped from the PR. Only a genuinely empty branch is a
            # no-op resolution (work already present elsewhere) that the job-level outcome should
            # report as "already complete".
            from shared_git.git_utils import (
                DEVELOPMENT_BRANCH,
                abort_merge,
                branch_diff,
                merge_branch,
            )

            branch = _orch._feature_branch_name(task)
            has_changes = bool((branch_diff(self.path, DEVELOPMENT_BRANCH, branch) or "").strip())
            if not has_changes:
                # Genuinely nothing landed — flag it resolved-without-changes so the job-level outcome
                # reports "already complete" rather than presenting a non-existent diff as merged work.
                self.graph.update_task(
                    task.id, resolved_without_changes=True, revision_feedback=feedback
                )
                self.graph.mark_branch_merged(task.id)
                return
            # merge_branch/abort_merge mutate the SHARED checkout (self.path) — a concurrent
            # `git checkout`+`git merge` from another worker's own no-change escalation in the
            # same round's fan-out (see orchestrator.run's parallel_map over active workers)
            # would race against this one on the same working directory/index. self._merge_lock
            # serializes only this git-mutating span so unrelated workers keep running fully
            # concurrently; _review_and_merge's own merges never overlap this fan-out (the round
            # loop runs it only after parallel_map returns), so it does not need this lock too.
            with self._merge_lock:
                try:
                    merged_ok, _ = merge_branch(self.path, branch, DEVELOPMENT_BRANCH)
                except Exception as e:  # noqa: BLE001 — a raised merge is a failed merge, handled below
                    logger.warning("Merge of adjudicated-done branch %s raised: %s", task.id, e)
                    merged_ok = False
                if not merged_ok:
                    # The Tech Lead judged the work done, but its branch will not integrate (merge
                    # conflict / checkout failure). A failed `git merge` leaves DEVELOPMENT_BRANCH
                    # mid-merge (conflict markers / MERGE_HEAD), so abort it first — otherwise later
                    # tasks and the GitHub publish step would run on a dirty, conflicted checkout.
                    # abort_merge is best-effort: a harmless no-op when no merge is in progress
                    # (e.g. the checkout failed). Stays inside the lock — it mutates the same
                    # shared checkout the merge attempt just did.
                    abort_merge(self.path)
            if not merged_ok:
                # Then FAIL the task (and cascade to dependents) to surface the gap rather than
                # claim a success that never landed on ``development``.
                logger.warning(
                    "Adjudicated-done branch %s failed to merge into %s; aborted the merge and "
                    "marking FAILED, not merged",
                    task.id,
                    DEVELOPMENT_BRANCH,
                )
                self.graph.update_task(
                    task.id, status=TaskStatus.FAILED, revision_feedback=feedback
                )
                self._cascade_fail_dependents(task.id)
                return
            # Real work landed → NOT resolved-without-changes; the job publishes a real PR.
            self.graph.update_task(
                task.id, resolved_without_changes=False, revision_feedback=feedback
            )
            self.graph.mark_branch_merged(task.id)
            return
        if verdict == "continue":
            # One more bounded window: reset the no-change counter AND clear the recorded digest so
            # the next round is measured from a fresh baseline. Clearing last_change_digest is what
            # makes "continue" an actual window — otherwise a task whose branch is still unchanged
            # re-trips the cap on the very next round (acute at CODING_TEAM_NO_CHANGE_REVISIT_CAP=1)
            # and re-enters adjudication immediately; and because the escalation bounce does not bump
            # revision_count, that churn would not be bounded by MAX_TASK_REVISIONS. With the digest
            # cleared the engineer gets a genuine round to change the code before any re-escalation.
            self.graph.update_task(
                task.id,
                status=TaskStatus.IN_PROGRESS,
                no_change_revisits=0,
                last_change_digest="",
                revision_feedback=feedback,
            )
            return
        # "fail" (and any unexpected verdict — run_revision_adjudication fails closed to "fail").
        self.graph.update_task(task.id, status=TaskStatus.FAILED, revision_feedback=feedback)
        self._cascade_fail_dependents(task.id)

    def _escalate_if_no_change(
        self, task: Task, new_feedback: List[Dict[str, Any]], diff: Optional[str] = None
    ) -> bool:
        """Record this round's feedback + no-change progress, escalating to the Tech Lead at the cap.

        The single choke-point for the prologue every revision path shares
        (``_handle_incomplete_implementation``, ``_return_for_revision``, ``_request_revision``): the
        feedback must be persisted BEFORE the no-change check so that, if this round trips the cap, the
        Tech Lead adjudicates over the full history including this round's reason. Persisting it here
        also means the caller's later status write need not re-pass ``revision_feedback``.

        Preconditions:
            - ``task`` is a non-terminal task tracked by this swarm's graph, being bounced this round.
            - ``new_feedback`` is this round's feedback entries (one or more); ``diff`` is the task's
              already-computed branch diff to reuse, or None to compute it from the branch.
        Postconditions:
            - ``new_feedback`` is appended to ``task.revision_feedback`` and the no-change
              digest/counter are updated, both persisted via the graph.
            - Returns True iff the no-change cap was reached and the task was handed to the Tech Lead
              (the caller must stop and not bounce the task itself); False otherwise.
        """
        self.graph.update_task(
            task.id, revision_feedback=list(task.revision_feedback or []) + list(new_feedback)
        )
        if self._note_revision_progress(task, diff=diff):
            self._escalate_to_tech_lead(self.graph.get_task(task.id) or task)
            return True
        return False

    def _handle_incomplete_implementation(self, task: Task, result: Dict[str, Any]) -> None:
        """Bound an implementation that did not reach review so it cannot spin the loop to max_rounds.

        Covers both status="failed" (e.g. the LLM call raised) and status="in_progress" (the model
        set ready_for_review=false). Previously only "failed" was handled and "in_progress" was
        dropped entirely, leaving the task IN_PROGRESS and assigned — so the same call repeated every
        round until the round cap. Count each occurrence against the shared revision cap and, on
        exhaustion, fail the task (and its dependents) terminally with the reason recorded.
        """
        from coding_team import orchestrator as _orch

        if result.get("status") == "in_progress":
            reason = "Engineer did not mark the work ready for review"
        else:
            reason = f"Implementation failed: {result.get('error') or 'unknown error'}"
        entry = {
            "source": "engineer",
            "reason": reason,
            "requested_changes": [],
        }
        # Records the feedback and, if this is a no-change loop, escalates to the Tech Lead (the
        # feedback is now persisted, so the status writes below need not re-pass it).
        if self._escalate_if_no_change(task, [entry]):
            return
        revision_count = task.revision_count + 1
        if revision_count >= _orch.MAX_TASK_REVISIONS:
            logger.warning(
                "Task %s did not reach review and exhausted revisions (%d); marking FAILED",
                task.id,
                _orch.MAX_TASK_REVISIONS,
            )
            self.graph.update_task(
                task.id,
                status=TaskStatus.FAILED,
                revision_count=revision_count,
            )
            self._cascade_fail_dependents(task.id)
            return
        # Keep it with the same engineer for another bounded attempt; record the reason.
        self.graph.update_task(
            task.id,
            status=TaskStatus.IN_PROGRESS,
            revision_count=revision_count,
        )

    def _escalate_decision(self, task: Task, result: Dict[str, Any], update_fn: Any) -> None:
        """Pause the job for a user decision a worker raised, then thread the answer back to the task.

        The engineer never decides the question itself. The task stays with the same engineer
        (IN_PROGRESS) so it re-implements next round with the user's decision in its feedback. An
        escalation is NOT counted against the revision cap — a late-stage question (a task already
        near the cap) must still get its answer implemented, not discarded. Pathological re-asking
        is bounded by the human (each escalation needs a user answer) and the swarm's round cap. A
        pause that ends without answers (terminal/timeout) aborts the swarm.

        Postconditions:
            - On a successful pause the task is IN_PROGRESS with a ``user_decision`` feedback entry
              and the same engineer, so the answer is implemented next round (revision count
              unchanged). On an unanswered pause ``self.aborted`` is set. With no answer channel the
              task is FAILED (fail closed).

        Concurrency:
            The pause cycle stores exactly one outstanding batch in job-level
            ``pending_questions``/``waiting_for_answers`` fields (see
            ``coding_team.pause_cycle._run_pause_cycle``) — it has no per-caller isolation. If two
            workers running concurrently (see the round fan-out in ``orchestrator.run``) both called
            ``pause_for_questions`` at once, the second call's questions would overwrite the
            first's, and a single answer submission would release both waiters even though only one
            batch's answers were actually recorded — the other would resume with an empty/mismatched
            resolution. ``self._pause_lock`` (held only around the ``pause_for_questions`` call, not
            this whole method) serializes the pause-and-resume round-trip across workers so each
            escalation's questions are posted, answered, and resolved before the next one starts;
            workers that never escalate a decision are unaffected and keep running fully concurrently.
        """
        from coding_team import orchestrator as _orch

        questions = result.get("open_questions") or []
        if self.pause_for_questions is None:
            # No answer channel wired (should not happen on real paths). Fail closed rather than
            # let the engineer's unanswered decision slip through as silently-decided work.
            logger.error(
                "Worker raised a decision but no pause channel is available; failing task %s",
                task.id,
            )
            self.graph.update_task(
                task.id,
                status=TaskStatus.FAILED,
                revision_feedback=list(task.revision_feedback or [])
                + [
                    {
                        "source": "system",
                        "reason": "engineer needs a product decision but no answer channel is available",
                        "requested_changes": [],
                    }
                ],
            )
            self._cascade_fail_dependents(task.id)
            return
        update_fn(status_text=f"Awaiting user decision: {task.title}")
        # Serialize the pause round-trip itself (see the Concurrency note above) — a concurrent
        # second escalation blocks here until this one is fully answered and resolved, then runs
        # its own pause cycle from a clean slate.
        with self._pause_lock:
            resolved, ok = self.pause_for_questions(
                questions, f"engineer:{task.assigned_agent_id or task.id}"
            )
        if not ok:
            self.aborted = True
            return
        feedback = list(task.revision_feedback or []) + [
            {
                "source": "user_decision",
                "reason": _format_decisions(resolved),
                "requested_changes": [],
                # Keep the structured records alongside the rendered reason so the review gates can
                # tell the reviewer the question is already settled (see _user_decisions_for). The
                # engineer-facing render reads "reason" and is unaffected.
                "decisions": resolved,
            }
        ]
        # Bound the total number of decision escalations per task, counted independently of the
        # revision cap (so a task at the revision cap still gets its decision implemented). This is a
        # cumulative per-task ceiling, not a same-question repeat detector: after MAX_TASK_REVISIONS
        # escalations the task is failed rather than pausing a human indefinitely. A task that
        # genuinely needs that many distinct decisions is over-scoped and should be split — at the
        # default (20) this needs 19 prior escalations, so it does not bite well-scoped tasks.
        prior_escalations = sum(
            1
            for e in (task.revision_feedback or [])
            if isinstance(e, dict) and e.get("source") == "user_decision"
        )
        if prior_escalations + 1 >= _orch.MAX_TASK_REVISIONS:
            logger.warning(
                "Task %s exceeded %d decision escalations; marking FAILED",
                task.id,
                _orch.MAX_TASK_REVISIONS,
            )
            self.graph.update_task(task.id, status=TaskStatus.FAILED, revision_feedback=feedback)
            self._cascade_fail_dependents(task.id)
            return
        self.graph.update_task(
            task.id,
            status=TaskStatus.IN_PROGRESS,
            revision_feedback=feedback,
        )

    def _run_quality_gates(
        self,
        swe: Any,
        task: Task,
        update_fn: Any,
        *,
        worktree_path: Path,
        live_progress: bool = True,
    ) -> bool:
        """Run build and lint against the worker's own worktree.

        The Tech Lead's diff-grounded review (``swarm_review._compute_review``) is the swarm's
        sole code-review signal — this gate previously also ran its own LLM code review here
        (on the same summary+diff evidence ``_compute_review`` builds), which was pure redundant
        cost: two full review calls over the same evidence, only one of which (the Tech Lead's)
        actually gates merge. Removed; build/lint remain because they are cheap, mechanical, and
        not redundant with anything downstream.

        Preconditions:
            - ``worktree_path`` is this worker's prepared git worktree (the same path
              ``run_implement`` just wrote to and checked out its feature branch on) — build and
              lint must read the files this worker actually produced, not the shared checkout.
            - ``live_progress=False`` suppresses the direct ``update_fn`` status-text calls —
              required when this call is part of a concurrent round fan-out, where concurrent
              status writes would race (mirrors ``_review_and_merge``'s fan-out suppression).
              ``True`` (default) keeps today's live per-phase progress for the serial/solo path.
        Postconditions:
            - Returns True if passed, False if returned for revision.
        """
        # The gate *tools* (build/lint) run inside the try so a tool crash never aborts the
        # swarm. The revision bookkeeping (_return_for_revision, which mutates the task graph)
        # is deliberately kept OUT of that try: if it raised inside the broad except, a build
        # REJECTION would be swallowed and reported as a gate PASS, merging unverified code. We
        # only record the verdict here and act on it after the try/except.
        revision_feedback: Optional[List[Dict[str, Any]]] = None
        try:
            provider = self.engine_provider
            if provider is None:
                # Skipping build/lint is never a silent event: production paths always inject a
                # provider (worker construction fails without one), so reaching this branch means
                # an embedder wired the swarm directly — surface it in the log AND the job record
                # so unverified merges are visible, not discovered post-hoc.
                logger.warning(
                    "No engine provider configured; skipping quality gates for %s", task.id
                )
                if live_progress:
                    update_fn(
                        status_text=f"Quality gates SKIPPED (no engine provider): {task.title}"
                    )
                return True
            run_build_verification = provider.run_build_verification
            run_linting = provider.run_linting

            agent_type = _quality_gate_agent_type(swe.stack_spec.name)

            # Build verification
            if live_progress:
                update_fn(status_text=f"Build verification: {task.title}")
            build = run_build_verification(worktree_path, agent_type, task.id)
            if not build.success:
                logger.warning(
                    "[%s] Build failed for task %s: %s", swe.agent_id, task.id, build.error
                )
                revision_feedback = [{"type": "build", "error": build.error}]
            else:
                # Linting
                if live_progress:
                    update_fn(status_text=f"Linting: {task.title}")
                run_linting(worktree_path, task.id, llm_getter=self.llm_getter)

        except Exception:
            # Log the full traceback, not a one-line summary: a real bug in the
            # review path (e.g. an OOM-precursor or a malformed evidence payload)
            # must be debuggable. With the engines injected, an ImportError here
            # means the provider's engine stack is broken — that deserves the
            # same full-stack ERROR, not a silent "tools not available" skip.
            # The swarm still proceeds — a failed gate must never abort the
            # whole run — but the stack is now in the logs.
            logger.exception("Quality gate tools error for task %s; proceeding", task.id)

        if revision_feedback is not None:
            return self._return_for_revision(task, revision_feedback)
        return True
