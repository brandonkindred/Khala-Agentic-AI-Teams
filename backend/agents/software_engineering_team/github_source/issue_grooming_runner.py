"""
IssueGroomingRunner: drives GitHub issue grooming Phase A (heuristic Fibonacci
scoring) then Phase B (sub-issue splitting) for one issue, reporting incremental
progress to the coding-team job store.

This is the business-logic layer the Temporal activity (``temporal.issue_grooming_workflow.
run_issue_grooming_activity``) wraps with a heartbeat and terminal-failure handling; nothing
here talks to Temporal.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from software_engineering_team.models import JobStatus

from .client import GitHubClient
from .issue_grooming_scoring import (
    ScoreBreakdown,
    inject_complexity_block,
    merge_complexity_label,
    score_issue,
)
from .issue_grooming_split import (
    build_sub_issue,
    extract_checklist_items,
    inject_sub_issues_block,
    plan_sub_issue_items,
    should_split,
)

# Mirrors coding_team_orchestrator.CANCEL_KEY -- the coding-team job-cancellation
# convention (a job field the cancel endpoint sets, polled cooperatively by
# long-running work rather than pushed). Inlined rather than imported to avoid
# pulling the whole orchestrator module into this lightweight runner.
_CANCEL_KEY = "cancel_requested"


class IssueGroomingRunner:
    """Drives Phase A -> Phase B GitHub issue grooming for one issue.

    Invariants:
        - Never calls ``activity.heartbeat`` or any Temporal API -- liveness is
          the caller's (the Temporal activity's) responsibility via
          ``BackgroundHeartbeat``; this class only reports progress through
          ``update_job_fn``.
        - Writes the job's terminal status itself on both the cancelled and
          completed paths (mirroring ``run_coding_team_orchestrator`` owning its
          own terminal status) -- the caller only needs to mark the job FAILED
          when ``run`` raises.
    """

    def __init__(
        self,
        client: GitHubClient,
        *,
        update_job_fn: Optional[Callable[..., None]] = None,
        get_job_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    ) -> None:
        """
        Preconditions:
            - ``client`` is a constructed ``GitHubClient`` (token already resolved).
            - ``update_job_fn``, when given, is already bound to this run's
              ``job_id`` (mirrors ``_grooming_update_callback`` -- ``run()`` never
              passes ``job_id`` itself) and accepts arbitrary keyword-only
              arguments forwarded verbatim to the job store's ``update_job``. In
              this class the kwargs it is actually called with are a subset of
              ``phase``, ``status_text``, ``progress``, ``grooming`` (a dict),
              and ``status`` (a ``JobStatus`` value, only on the terminal
              ``CANCELLED``/``COMPLETED`` calls) -- never positional args, and
              never all five on every call.
        Postconditions:
            - Stores ``client``/``update_job_fn``/``get_job_fn`` for ``run()``. A
              missing ``update_job_fn`` makes progress reporting a no-op (useful
              for exercising the runner in isolation); a missing ``get_job_fn``
              disables the mid-run cancellation check.
        """
        self._client = client
        self._update_job_fn = update_job_fn or (lambda **_kw: None)
        self._get_job_fn = get_job_fn

    def _cancel_requested(self, job_id: str) -> bool:
        if self._get_job_fn is None:
            return False
        job = self._get_job_fn(job_id)
        return bool(job and job.get(_CANCEL_KEY))

    @staticmethod
    def _grooming_dict(score: ScoreBreakdown, children: List[Tuple[int, str]]) -> Dict[str, Any]:
        return {
            "score": score.model_dump(),
            "sub_issues": [{"number": number, "title": title} for number, title in children],
        }

    def run(self, job_id: str, owner: str, repo: str, issue_number: int) -> Dict[str, Any]:
        """Run Phase A then (conditionally) Phase B grooming for one issue.

        Preconditions:
            - ``owner``/``repo``/``issue_number`` name an existing, accessible
              GitHub issue; ``self._client`` is authorized for ``owner/repo``.
        Postconditions:
            - Always runs Phase A: scores the issue (:func:`score_issue`),
              injects the complexity table into its body, merges the
              ``complexity: N`` label, and PATCHes the issue. Reports progress
              via ``update_job_fn`` after each step.
            - If a cancellation is observed after Phase A (``update_job_fn``'s
              job carries a truthy ``cancel_requested`` field), marks the job
              ``CANCELLED`` and returns the Phase-A-only ``grooming`` dict
              without running Phase B.
            - Otherwise runs Phase B when :func:`should_split` is True AND the
              issue has no sub-issues yet (:meth:`GitHubClient.list_sub_issues`
              -- the one non-idempotent guard: a re-run against an
              already-split issue leaves Phase B untouched rather than creating
              duplicate children). Phase B also polls ``cancel_requested``
              before creating each sub-issue (not just once before the loop)
              and reports progress after every child created, so a cancellation
              mid-split stops promptly -- with whatever children were already
              created kept in the returned ``grooming`` -- instead of finishing
              the whole batch, and a long split no longer looks stuck between
              Phase A's 40% and the post-loop 90%. Marks the job ``COMPLETED``
              once Phase B (or the decision to skip it) is done.
            - Returns the final ``grooming`` dict: ``{"score": ...}`` when Phase
              B did not run, or ``{"score": ..., "sub_issues": [...]}`` when it
              did -- the same shape written via the last ``update_job_fn(grooming=...)``
              call.
            - Raises whatever ``self._client``'s methods raise
              (``GitHubAPIError``) on any GitHub failure; does not catch or
              translate them; does not itself write a ``FAILED`` status -- that
              is the Temporal activity wrapper's responsibility.
        """
        update = self._update_job_fn
        update(phase="fetching", status_text="Fetching issue", progress=10)
        issue = self._client.get_issue(owner, repo, issue_number)

        score = score_issue(issue.title, issue.body)
        scored_body = inject_complexity_block(issue.body, score)
        new_labels = merge_complexity_label(issue.labels, score)
        self._client.update_issue(owner, repo, issue_number, body=scored_body, labels=new_labels)
        grooming: Dict[str, Any] = {"score": score.model_dump()}
        update(
            phase="phase_a", status_text="Scored issue complexity", progress=40, grooming=grooming
        )

        if self._cancel_requested(job_id):
            update(
                status=JobStatus.CANCELLED.value, phase="cancelled", status_text="Cancelled by user"
            )
            return grooming

        checklist_items = extract_checklist_items(issue.body)
        if should_split(score, checklist_items) and not self._client.list_sub_issues(
            owner, repo, issue_number
        ):
            children: List[Tuple[int, str]] = []
            planned_items = plan_sub_issue_items(checklist_items)
            total = len(planned_items)
            for index, item_text in enumerate(planned_items, start=1):
                if self._cancel_requested(job_id):
                    grooming = self._grooming_dict(score, children)
                    update(
                        status=JobStatus.CANCELLED.value,
                        phase="cancelled",
                        status_text="Cancelled by user",
                        grooming=grooming,
                    )
                    return grooming
                child_title, child_body = build_sub_issue(
                    issue, item_text, index=index, total=total
                )
                child = self._client.create_issue(owner, repo, title=child_title, body=child_body)
                self._client.add_sub_issue(owner, repo, issue_number, sub_issue_id=child.id)
                children.append((child.number, child.title))
                update(
                    phase="phase_b",
                    status_text=f"Creating sub-issue {index}/{total}",
                    progress=40 + 50 * index // total,
                    grooming=self._grooming_dict(score, children),
                )
            split_body = inject_sub_issues_block(scored_body, children)
            self._client.update_issue(owner, repo, issue_number, body=split_body)
            grooming = self._grooming_dict(score, children)
            update(
                phase="phase_b",
                status_text=f"Split into {len(children)} sub-issue(s)",
                progress=90,
                grooming=grooming,
            )

        update(
            status=JobStatus.COMPLETED.value,
            phase="done",
            status_text="Grooming complete",
            progress=100,
            grooming=grooming,
        )
        return grooming
