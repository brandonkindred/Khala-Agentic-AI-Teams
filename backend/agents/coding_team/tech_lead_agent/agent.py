"""
Tech Lead agent: plan → Task Graph + stacks; groom tasks; assignments; code review.
Orchestrator performs actual Task Graph updates and git merge.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

from strands import Agent

from coding_team.hitl import normalize_open_questions as _normalize_open_questions
from coding_team.hitl import resolved_decision_lines
from coding_team.models import CodingTeamPlanInput
from coding_team.tech_lead_agent import prompts
from llm_service import call_llm_with_retries

logger = logging.getLogger(__name__)


def _review_retry_attempts() -> int:
    """Total review attempts (retries + 1) before giving up.

    A reviewer call can fail for transient reasons (rate limit, network timeout, provider
    outage) that say nothing about the implementation and are usually recoverable, so we retry
    with backoff rather than failing the task on the first error. Configurable via
    CODING_TEAM_REVIEW_RETRIES (default 2 retries → 3 attempts; garbage/negative → default;
    floored at 1 attempt).
    """
    raw = os.environ.get("CODING_TEAM_REVIEW_RETRIES")
    try:
        retries = int(raw) if raw is not None and raw.strip() != "" else 2
    except (TypeError, ValueError):
        retries = 2
    # A negative value is meaningless as a retry count; rather than silently collapsing it to a
    # single attempt (which would strip all transient-failure protection for a disabling-style
    # value like -1), treat it as garbage and restore the documented default.
    if retries < 0:
        retries = 2
    return retries + 1


def _render_resolved_questions(resolved: List[Dict[str, Any]]) -> str:
    """Render user-supplied decisions as 'question → answer' lines for the planning prompt.

    Preconditions:
        - ``resolved`` entries are dicts; an answer carries ``question_text`` and a human-readable
          ``answer`` (or ``selected_answer`` / ``selected_option_id`` / ``other_text``).
    Postconditions:
        - Returns a non-empty bullet string when any decision has content, else "".
    """
    return "\n".join(f"- {line}" for line in resolved_decision_lines(resolved))


def _plan_text(plan: CodingTeamPlanInput) -> str:
    """Build plan text for the LLM from plan input.

    Every field is passed through in full: the plan, spec, and architecture are the primary inputs
    the Tech Lead reasons over, and clipping them would hide requirements from the Task Graph. The
    user's resolved decisions and recorded assumptions are included too, so the Tech Lead breaks the
    plan down according to choices the user actually made, not ones an agent invented. Unanswered
    open questions are NOT rendered here: by the time this runs the orchestrator's decision gate has
    guaranteed there are none (the job pauses otherwise). Inputs are never truncated.

    Postconditions:
        - Each non-empty plan field (including resolved decisions and assumptions) appears verbatim
          (uncut) in the returned text.
    """
    parts = [
        f"Title: {plan.requirements_title}",
        f"Description: {plan.requirements_description}" if plan.requirements_description else "",
    ]
    if plan.project_overview:
        parts.append("Project overview: " + json.dumps(plan.project_overview, indent=2))
    if plan.final_spec_content:
        parts.append("Spec: " + plan.final_spec_content)
    if plan.architecture_overview:
        parts.append("Architecture: " + plan.architecture_overview)
    resolved_text = _render_resolved_questions(plan.resolved_questions or [])
    if resolved_text:
        parts.append(
            "User decisions (these were answered by the user — implement them exactly, "
            "do not revisit):\n" + resolved_text
        )
    if plan.assumptions:
        parts.append("Assumptions on record:\n" + "\n".join(f"- {a}" for a in plan.assumptions))
    if plan.completed_work_summary:
        # Evidence that the plan's work is already finished (e.g. closed/merged sub-issues). This is
        # the signal the Tech Lead uses to return already_complete instead of inventing tasks to redo
        # finished work. Kept distinct from existing_code_summary — only genuinely-done work, never
        # ordinary repo context, may justify an already_complete verdict.
        parts.append(
            "Work already completed (already merged/done — do NOT recreate it):\n"
            + plan.completed_work_summary
        )
    if plan.existing_code_summary:
        # Existing repository code, surfaced purely as CONTEXT. It is current code the plan may well
        # need to modify, so it is framed explicitly as "not completed work" — its mere presence must
        # never on its own be read as "already done" (that is what drove a false already_complete on
        # the main software-engineering path, where this field carries the whole repo's source).
        parts.append(
            "Existing repository code (context only — the plan may require changing it; this is NOT "
            "completed work):\n" + plan.existing_code_summary
        )
    return "\n\n".join(p for p in parts if p)


def _agent_call_json(agent: Agent, prompt: str) -> Dict[str, Any]:
    """Call a Strands Agent and parse the result as JSON."""
    result = agent(prompt)
    raw = str(result).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


class TechLeadAgent:
    """Tech Lead: given plan, produce tasks + stacks; groom tasks; suggest assignments; code review."""

    def __init__(self, model: Any) -> None:
        self._model = model
        self._plan_agent = Agent(model=model, system_prompt=prompts.PLAN_TO_TASK_GRAPH_SYSTEM)
        self._groom_agent = Agent(model=model, system_prompt=prompts.GROOM_TASK_SYSTEM)
        self._assignment_agent = Agent(model=model, system_prompt=prompts.ASSIGNMENT_SYSTEM)
        self._review_agent = Agent(model=model, system_prompt=prompts.CODE_REVIEW_SYSTEM)
        self._adjudication_agent = Agent(
            model=model, system_prompt=prompts.REVISION_ADJUDICATION_SYSTEM
        )

    def run_plan_to_task_graph(self, plan: CodingTeamPlanInput) -> Dict[str, Any]:
        """
        Given plan from Planning team, return { "tasks": [...], "stacks": [...], "open_questions": [...] }.
        Orchestrator will add tasks to Task Graph and create Senior SWEs from stacks. A non-empty
        ``open_questions`` means the Tech Lead needs a product/design decision it must not make
        itself; the orchestrator pauses the job for the user rather than building tasks.

        Postconditions:
            - Returns a dict with "tasks" (possibly empty), a non-empty "stacks" (defaulted),
              "open_questions" (possibly empty), "already_complete" (bool), and
              "completion_evidence" (str). Never decides an open question on the caller's behalf.
            - "already_complete" is True only when the work the plan describes is already finished;
              the caller short-circuits to a clean terminal outcome instead of building tasks.
        """
        plan_text = _plan_text(plan)
        user = prompts.PLAN_TO_TASK_GRAPH_USER.format(plan_text=plan_text)
        user += "\n\nRespond with valid JSON only, no markdown fences."
        try:
            data = _agent_call_json(self._plan_agent, user)
        except Exception as e:
            logger.warning("Tech Lead plan_to_task_graph LLM failed: %s", e)
            return {
                "tasks": [],
                "stacks": [{"name": "default", "tools_services": []}],
                "open_questions": [],
                "already_complete": False,
                "completion_evidence": "",
            }
        tasks_raw = data.get("tasks") or []
        stacks_raw = data.get("stacks") or []
        tasks = []
        for t in tasks_raw:
            if isinstance(t, dict) and t.get("id"):
                tasks.append(
                    {
                        "id": str(t["id"]),
                        "title": t.get("title", t["id"]),
                        "description": t.get("description", ""),
                        "dependencies": list(t.get("dependencies") or []),
                    }
                )
        stacks = []
        for s in stacks_raw:
            if isinstance(s, dict):
                name = s.get("name") or "stack"
                tools = s.get("tools_services")
                if not isinstance(tools, list):
                    tools = []
                stacks.append({"name": name, "tools_services": [str(x) for x in tools]})
        if not stacks:
            stacks = [{"name": "default", "tools_services": []}]
        # already_complete only counts when the model also returned no tasks: a true flag alongside
        # a non-empty task list is contradictory, so the tasks win (we never silently drop work).
        already_complete = bool(data.get("already_complete")) and not tasks
        return {
            "tasks": tasks,
            "stacks": stacks,
            "open_questions": _normalize_open_questions(data.get("open_questions")),
            "already_complete": already_complete,
            "completion_evidence": str(data.get("completion_evidence") or "")
            if already_complete
            else "",
        }

    def run_revision_adjudication(
        self,
        task_title: str,
        task_description: str,
        acceptance_criteria: List[str],
        changes_summary: str,
        revision_feedback: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Give direction on a task stuck in a no-change revision loop.

        Called when an engineer has revisited a task several rounds in a row without changing the
        code. The accumulated ``revision_feedback`` plus ``changes_summary`` is the documentation of
        what has been tried; the Tech Lead reads it and decides whether the work is already done,
        genuinely cannot be completed, or has a concrete remaining change worth one more window.

        Preconditions:
            - ``revision_feedback`` is the task's accumulated bounce history (possibly empty).
        Postconditions:
            - Returns ``{"verdict": "done"|"fail"|"continue", "reason": str}``. On any LLM/parse
              failure, returns ``verdict="fail"`` with the diagnostic in ``reason`` — a stuck task
              that cannot even be adjudicated must not be re-fed into the loop.
        """
        feedback_text = json.dumps(revision_feedback or [], indent=2)
        user = prompts.REVISION_ADJUDICATION_USER.format(
            task_title=task_title,
            task_description=task_description,
            acceptance_criteria=json.dumps(acceptance_criteria),
            changes_summary=changes_summary or "(no changes recorded)",
            revision_feedback=feedback_text,
        )
        user += "\n\nRespond with valid JSON only, no markdown fences."
        try:
            data = call_llm_with_retries(
                lambda: _agent_call_json(self._adjudication_agent, user),
                max_attempts=_review_retry_attempts(),
            )
        except Exception as e:  # noqa: BLE001 — a failed adjudication must not re-enter the loop
            logger.warning("Tech Lead revision adjudication failed: %s", e)
            return {
                "verdict": "fail",
                "reason": f"Could not adjudicate the stalled task: {e}",
            }
        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict not in ("done", "fail", "continue"):
            # An unusable verdict is not a license to keep spinning — fail closed so the stuck task
            # terminates with a recorded diagnostic rather than looping.
            logger.warning("Tech Lead returned an unusable adjudication verdict: %r", data)
            return {
                "verdict": "fail",
                "reason": f"Adjudication returned an unusable verdict: {data!r}",
            }
        return {"verdict": verdict, "reason": str(data.get("reason") or "")}

    def run_groom_task(
        self,
        task_id: str,
        task_title: str,
        task_description: str,
        task_dependencies: List[str],
        plan_context: str,
    ) -> Dict[str, Any]:
        """Groom one task: acceptance criteria, out of scope, enriched description, priority, subtasks."""
        user = prompts.GROOM_TASK_USER.format(
            task_id=task_id,
            task_title=task_title,
            task_description=task_description,
            task_dependencies=json.dumps(task_dependencies),
            plan_context=plan_context,
        )
        user += "\n\nRespond with valid JSON only, no markdown fences."
        try:
            data = _agent_call_json(self._groom_agent, user)
        except Exception as e:
            logger.warning("Tech Lead groom_task LLM failed: %s", e)
            return {
                "acceptance_criteria": [],
                "out_of_scope": "",
                "description_enriched": task_description,
                "priority": "medium",
                "subtasks": [],
                "task_dependencies": task_dependencies,
            }
        return {
            "acceptance_criteria": list(data.get("acceptance_criteria") or []),
            "out_of_scope": str(data.get("out_of_scope") or ""),
            "description_enriched": str(data.get("description_enriched") or task_description),
            "priority": str(data.get("priority") or "medium"),
            "subtasks": list(data.get("subtasks") or []),
            "task_dependencies": list(data.get("task_dependencies") or task_dependencies),
        }

    def run_assignments(
        self,
        agent_ids: List[str],
        ready_tasks: List[Dict[str, Any]],
        free_agents: List[str],
    ) -> Dict[str, Any]:
        """Suggest assignments: list of { agent_id, task_id }. Orchestrator calls Task Graph assign."""
        user = prompts.ASSIGNMENT_USER.format(
            agent_ids=json.dumps(agent_ids),
            ready_tasks=json.dumps(ready_tasks),
            free_agents=json.dumps(free_agents),
        )
        user += "\n\nRespond with valid JSON only, no markdown fences."
        try:
            data = _agent_call_json(self._assignment_agent, user)
        except Exception as e:
            logger.warning("Tech Lead assignments LLM failed: %s", e)
            return {"assignments": []}
        assignments = data.get("assignments") or []
        return {
            "assignments": [
                a
                for a in assignments
                if isinstance(a, dict) and a.get("agent_id") and a.get("task_id")
            ]
        }

    def run_code_review(
        self,
        task_title: str,
        task_description: str,
        acceptance_criteria: List[str],
        changes_summary: str,
        user_decisions: Optional[List[str]] = None,
        progress_callback: Optional[Callable[[str, str, float], None]] = None,
    ) -> Dict[str, Any]:
        """Review feature branch: approved (bool), reason (str), requested_changes (list).

        Preconditions:
            - ``user_decisions`` is None or a list of human-readable decision lines
              (``"question → answer"``) the user has already answered; when non-empty they are
              surfaced to the reviewer as settled facts so it never re-raises a question the user
              has decided. Empty/None adds nothing to the prompt (identical to the prior behavior).
            - ``progress_callback`` is None or a callable accepting
              ``(step, detail, fraction)``; steps emitted here are
              ``reviewing | waiting_retry | done``. Exceptions it raises are
              logged and swallowed — they must never count as failed attempts.

        Postconditions:
            - When ``progress_callback`` is provided, each LLM attempt and each
              backoff wait is reported, and a terminal ``done`` report at 1.0 is
              emitted on both the success and the exhausted-retries paths.
            - The review result is identical whether or not a callback is provided.
        """
        user = prompts.CODE_REVIEW_USER.format(
            task_title=task_title,
            task_description=task_description,
            acceptance_criteria=json.dumps(acceptance_criteria),
            changes_summary=changes_summary,
        )
        decisions = [str(d).strip() for d in (user_decisions or []) if str(d).strip()]
        if decisions:
            # Appended (not a CODE_REVIEW_USER placeholder) so the template's .format() keys are
            # untouched. Mirrors the planning path's "User decisions" block in _plan_text.
            user += (
                "\n\nUser decisions already made (answered by the user — these are settled; "
                "do NOT request changes to revisit them or treat them as open/unanswered "
                "questions):\n" + "\n".join(f"- {d}" for d in decisions)
            )
        user += "\n\nRespond with valid JSON only, no markdown fences."
        attempts = _review_retry_attempts()

        def _report(step: str, detail: str, fraction: float) -> None:
            # Guarded here, not just documented: _report fires INSIDE the retried
            # attempt, so a raising callback would otherwise be miscounted as a
            # failed LLM attempt and burn the whole retry budget on an
            # observability bug without the model ever being called.
            if progress_callback is None:
                return
            try:
                progress_callback(step, detail, fraction)
            except Exception as e:  # noqa: BLE001 — observability must not break the review
                logger.warning("review progress callback failed (ignored): %s", e)

        attempt_no = 0

        def _attempt_review() -> Dict[str, Any]:
            nonlocal attempt_no
            attempt_no += 1
            _report(
                "reviewing",
                f"attempt {attempt_no}/{attempts}",
                min(0.1 + 0.8 * (attempt_no - 1) / attempts, 0.9),
            )
            data = _agent_call_json(self._review_agent, user)
            # A response that parses as JSON but carries no verdict (missing/null "approved") is not
            # a substantive rejection — it's an unusable review. Raise so the shared retry envelope
            # retries it and it ultimately surfaces error=True (fail once), rather than silently
            # becoming approved=False and burning the revision loop.
            if data.get("approved") is None:
                raise ValueError(f"review response missing 'approved' verdict: {data!r}")
            return data

        # Reuse the platform's jittered-exponential-backoff retry envelope rather than a bespoke
        # loop, so review retry/backoff tuning stays consistent with every other LLM caller.
        try:
            data = call_llm_with_retries(
                _attempt_review,
                max_attempts=attempts,
                on_retry=lambda n, m, wait, e: _report(
                    "waiting_retry",
                    f"attempt {n}/{m} failed; retrying in {wait:.0f}s",
                    min(0.1 + 0.8 * n / m, 0.9),
                ),
            )
        except Exception as e:  # noqa: BLE001 — a failed review must never abort the swarm
            # Flag an infrastructure failure (error=True) distinctly from a substantive rejection so
            # the orchestrator fails the task once with a clear diagnostic rather than re-sending the
            # same failing prompt through the revision loop every round.
            # attempt_no, not the configured budget: fail-fast errors (rate limit,
            # permanent) re-raise on attempt 1 without retrying, and reporting
            # "after 3 attempts" for a single attempt misleads the operator.
            logger.warning("Tech Lead code_review failed after %d attempt(s): %s", attempt_no, e)
            _report("done", f"review failed after {attempt_no} attempt(s)", 1.0)
            return {
                "approved": False,
                "error": True,
                "reason": f"Review could not be completed after {attempt_no} attempt(s): {e}",
                "requested_changes": [],
            }
        _report("done", "review complete", 1.0)
        return {
            "approved": bool(data.get("approved")),
            "error": False,
            "reason": str(data.get("reason") or ""),
            "requested_changes": list(data.get("requested_changes") or []),
        }
