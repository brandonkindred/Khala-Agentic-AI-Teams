"""
Tech Lead agent: plan → Task Graph + stacks; groom tasks; assignments; code review.
Orchestrator performs actual Task Graph updates and git merge.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Collection, Dict, List, Optional

from strands import Agent

from llm_service import LLMJsonParseError, call_llm_with_retries, extract_json_from_response
from shared.env import parse_int
from shared.llm_recovery import extract_json_object
from software_engineering_team.hitl import (
    normalize_open_questions as _normalize_open_questions,
)
from software_engineering_team.hitl import resolved_decision_lines
from software_engineering_team.models import CodingTeamPlanInput
from software_engineering_team.tech_lead_agent import prompts

logger = logging.getLogger(__name__)


def _review_retry_attempts() -> int:
    """Total review attempts (retries + 1) before giving up.

    A reviewer call can fail for transient reasons (rate limit, network timeout, provider
    outage) that say nothing about the implementation and are usually recoverable, so we retry
    with backoff rather than failing the task on the first error. Configurable via
    CODING_TEAM_REVIEW_RETRIES (default 2 retries → 3 attempts; garbage/negative → default;
    floored at 1 attempt).

    Preconditions:
        - None; reads only the ``CODING_TEAM_REVIEW_RETRIES`` environment variable.
    Postconditions:
        - Returns an int >= 1. A parseable non-negative value yields ``retries + 1``; a
          missing/garbage/negative value falls back to the documented default of 2 retries
          (3 attempts).
    """
    retries = parse_int("CODING_TEAM_REVIEW_RETRIES", 2)
    # A negative value is meaningless as a retry count; rather than silently collapsing it to a
    # single attempt (which would strip all transient-failure protection for a disabling-style
    # value like -1), treat it as garbage and restore the documented default.
    if retries < 0:
        retries = 2
    return retries + 1


def _as_bool(value: Any) -> bool:
    """Coerce an LLM-provided flag to a strict boolean.

    JSON booleans already parse to ``bool``; this guards the common schema drift where a model emits
    the STRING "false"/"true". ``bool("false")`` is True, so a naive cast would read "false" as
    truthy — which for ``already_complete`` would wrongly short-circuit the whole job and recommend
    closing the issue with no PR. Only a real ``True`` or an explicit true-like string
    ("true"/"1"/"yes", case-insensitive) counts.

    Preconditions:
        - ``value`` is arbitrary parsed-JSON content (bool, str, number, None, ...).
    Postconditions:
        - Returns a bool; anything not unambiguously true (including "false"/"0"/"no"/None/other
          strings/numbers) returns False — the safe default that never treats a non-true value as
          true.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


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
    # NOTE: existing_code_summary (the main SE path fills it with the whole repository's source) is
    # deliberately NOT surfaced to the planner. Feeding repo code into the plan prompt risks a false
    # already_complete — ordinary existing code read as "already done" — and the planner worked
    # without it before this field was ever rendered. Only completed_work_summary (explicit
    # finished-work evidence) reaches the prompt and drives already_complete.
    return "\n\n".join(p for p in parts if p)


def _agent_call_json(
    agent: Agent, prompt: str, required_keys: Optional[Collection[str]] = None
) -> Dict[str, Any]:
    """Call a Strands Agent and parse the result as JSON.

    Preconditions:
        - ``agent`` is a callable Strands ``Agent``; ``prompt`` is a non-empty str.
        - ``required_keys`` is ``None`` or the anchor keys this call site expects
          in the payload (e.g. ``{"approved"}`` for a review verdict). When set,
          salvage only accepts a recovered object carrying at least one of them,
          so a usage/format echo that lacks the anchor cannot be mistaken for the
          answer.
    Postconditions:
        - Returns the parsed object via a three-tier ladder, each tier only tried
          after the previous one fails:
          1. Strict ``json.loads`` (after stripping a single leading/trailing
             ```` ``` ```` fence) for well-formed replies.
          2. The shared, non-blogging-aware ``extract_json_object`` salvage engine
             (``shared.llm_recovery`` — the same engine ``agent_call_json`` used),
             anchored on ``required_keys`` with correct last-candidate-wins
             handling of an echoed format example. This tier recovers the vast
             majority of prose-/fence-wrapped replies without ever reaching tier 3.
          3. The canonical ``extract_json_from_response`` as a final fallback, so
             this call site still benefits from any recovery capability unique to
             it. Tiers 1-2 exist specifically so well-formed or salvageable JSON
             never reaches tier 3's own pre-parse heuristics (e.g. its
             ``---DRAFT---`` shortcut, which scans raw text for that literal
             substring — including inside a JSON string value, such as a
             code-review reason quoting this repo's blog draft marker — and its
             first-fenced-block fast path, which does not honor ``required_keys``)
             on input that a safer engine could already handle correctly. Unlike
             tier 2, several of tier 3's own recovery paths neither guarantee a
             ``dict`` result (a fenced array-with-prose reply can come back as a
             bare ``list``) nor honor ``required_keys`` (an anchor-less object can
             win before its own key-anchored stage ever runs) — so tier 3's result
             is validated against both before being accepted; a result that fails
             either check is treated the same as "nothing recovered."
          Raises ``json.JSONDecodeError`` only when no object can be recovered by
          any tier — ``LLMJsonParseError`` from tier 3, and a tier-3 result that
          fails validation, are both raised as ``json.JSONDecodeError`` so callers
          keep retrying JSON-parse failures exactly as before (``LLMJsonParseError``
          is otherwise non-retryable in ``call_llm_with_retries``).
    """
    raw = str(agent(prompt)).strip()
    fenced = re.sub(r"^```(?:json)?\s*", "", raw)
    fenced = re.sub(r"\s*```$", "", fenced)
    try:
        return json.loads(fenced)
    except json.JSONDecodeError:
        pass
    recovered = extract_json_object(raw, required_keys=required_keys)
    if recovered is not None:
        return recovered
    expected_keys = frozenset(required_keys) if required_keys is not None else None
    try:
        fallback = extract_json_from_response(raw, expected_keys=expected_keys)
    except LLMJsonParseError as e:
        raise json.JSONDecodeError(str(e), raw, 0) from e
    if not isinstance(fallback, dict) or (expected_keys and not (expected_keys & fallback.keys())):
        raise json.JSONDecodeError(
            "extract_json_from_response fallback returned an object that fails "
            "this call site's dict/required_keys contract",
            raw,
            0,
        )
    return fallback


_JSON_ONLY_INSTRUCTION = "\n\nRespond with valid JSON only, no markdown fences."


def _call_json(
    agent: Agent,
    template: str,
    fmt_kwargs: Dict[str, Any],
    *,
    required_keys: Optional[Collection[str]] = None,
    default: Dict[str, Any],
    retries: bool = True,
    extra: str = "",
    label: str = "",
) -> Dict[str, Any]:
    """Format a prompt template, call the agent for JSON, and own the shared retry/fallback contract.

    Collapses the "format -> append JSON instruction -> call -> except: return default" scaffold
    duplicated across every Tech Lead JSON call site into one place, and is the single spot
    ``_JSON_ONLY_INSTRUCTION`` is appended.

    Preconditions:
        - ``template`` is one of the ``prompts.*_USER`` format strings; ``fmt_kwargs`` supplies
          exactly its placeholders.
        - ``extra`` is literal text the caller has already assembled (e.g. optional prompt
          sections) appended after formatting — it is NOT passed through ``.format()``, so
          caller-controlled content containing ``{``/``}`` (spec text, diffs) can never break
          formatting.
        - ``default`` is the dict this call site falls back to; consulted only when ``retries``
          is True.
    Postconditions:
        - When ``retries`` is True: wraps the call in ``call_llm_with_retries``
          (``max_attempts=_review_retry_attempts()``); on any exception surviving retries, logs a
          warning (annotated with ``label`` when given) and returns ``default`` unmodified — the
          caller never sees the exception.
        - When ``retries`` is False: makes exactly one attempt and does not catch — any exception
          propagates to the caller, which owns its own retry policy and failure shaping (used by
          call sites that already wrap this call in their own retry/backoff and need the raw
          exception for diagnostics). ``default`` is unused in this path.
    """
    prompt = template.format(**fmt_kwargs) + extra + _JSON_ONLY_INSTRUCTION
    call = lambda: _agent_call_json(agent, prompt, required_keys=required_keys)  # noqa: E731
    if not retries:
        return call()
    try:
        return call_llm_with_retries(call, max_attempts=_review_retry_attempts())
    except Exception as e:  # noqa: BLE001 — a failed call must fall back to a safe default
        logger.warning("Tech Lead %sLLM failed: %s", f"{label} " if label else "", e)
        return default


def _fallback_stack_specs() -> List[Dict[str, Any]]:
    """Canonical v2 roster used when planning can't determine real stacks.

    Neither failure mode (LLM call failed outright, or returned no usable stacks) carries any
    signal about which specialty is needed, so this returns both frontend_v2 and backend_v2
    rather than guessing one.
    """
    return [
        {
            "name": "frontend_v2",
            "tools_services": ["Angular", "TypeScript", "React", "CSS", "HTML"],
        },
        {
            "name": "backend_v2",
            "tools_services": ["Java", "Python", "Node.js", "Databases", "APIs", "DevOps"],
        },
    ]


class TechLeadAgent:
    """Tech Lead: given plan, produce tasks + stacks; groom tasks; suggest assignments; code review.

    Invariants:
        - ``_plan_agent``, ``_groom_agent``, ``_assignment_agent``, and ``_adjudication_agent``
          are fixed for the instance's lifetime and are each other's own conversation state;
          concurrent calls to different ``run_*`` methods do not share history.
        - ``run_code_review`` never uses a stored agent: it builds a fresh per-call ``Agent`` every
          invocation, so concurrent reviews (the orchestrator's review fan-out) cannot
          cross-contaminate each other's conversation history.
        - Each ``run_*`` method is independently callable any number of times and has no ordering
          requirement relative to the others.
    """

    def __init__(self, model: Any) -> None:
        """Construct the Tech Lead's fixed per-purpose agents.

        Preconditions:
            - ``model`` is a Strands-compatible model handle usable to construct an ``Agent``.
        Postconditions:
            - ``_plan_agent``, ``_groom_agent``, ``_assignment_agent``, and
              ``_adjudication_agent`` are each bound to a new ``Agent`` sharing ``model`` and the
              corresponding system prompt from ``prompts``. No review agent is created here;
              ``run_code_review`` builds its own per call (see class Invariants).
        """
        self._model = model
        self._plan_agent = Agent(model=model, system_prompt=prompts.PLAN_TO_TASK_GRAPH_SYSTEM)
        self._groom_agent = Agent(model=model, system_prompt=prompts.GROOM_TASK_SYSTEM)
        self._assignment_agent = Agent(model=model, system_prompt=prompts.ASSIGNMENT_SYSTEM)
        # No shared review agent: run_code_review builds a fresh Agent per call. A Strands Agent
        # accumulates conversation state (self.messages) across calls, so a single shared instance is
        # unsafe under the orchestrator's concurrent review fan-out — two reviews would race on the
        # same history and cross-contaminate each other's evidence. A per-call agent is independent.
        self._adjudication_agent = Agent(
            model=model, system_prompt=prompts.REVISION_ADJUDICATION_SYSTEM
        )

    def run_plan_to_task_graph(self, plan: CodingTeamPlanInput) -> Dict[str, Any]:
        """
        Given plan from Planning team, return { "tasks": [...], "stacks": [...], "open_questions": [...] }.
        Orchestrator will add tasks to Task Graph and create v2 implementation workers from stacks. A non-empty
        ``open_questions`` means the Tech Lead needs a product/design decision it must not make
        itself; the orchestrator pauses the job for the user rather than building tasks.
        ``target_team`` is read verbatim from each task; a task with no ``target_team`` gets
        ``""`` (swarm_assignment treats that as no team constraint, not a hard failure).

        Preconditions:
            - ``plan`` is a ``CodingTeamPlanInput`` carrying the plan/spec/architecture text the
              Tech Lead reasons over; any unanswered open questions have already been resolved by
              the caller (this method never re-raises them).
        Postconditions:
            - Returns a dict with "tasks" (possibly empty), a non-empty "stacks" (defaulted),
              "open_questions" (possibly empty), "already_complete" (bool), and
              "completion_evidence" (str). Never decides an open question on the caller's behalf.
            - "already_complete" is True only when the work the plan describes is already finished;
              the caller short-circuits to a clean terminal outcome instead of building tasks.
        """
        plan_text = _plan_text(plan)
        data = _call_json(
            self._plan_agent,
            prompts.PLAN_TO_TASK_GRAPH_USER,
            {"plan_text": plan_text},
            required_keys=("tasks", "stacks", "open_questions", "already_complete"),
            default={
                "tasks": [],
                "stacks": _fallback_stack_specs(),
                "open_questions": [],
                "already_complete": False,
                "completion_evidence": "",
            },
            label="plan_to_task_graph",
        )
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
                        "target_team": str(t.get("target_team") or "").strip(),
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
            stacks = _fallback_stack_specs()
        # already_complete only counts when the model also returned no tasks: a true flag alongside
        # a non-empty task list is contradictory, so the tasks win (we never silently drop work).
        # Use strict boolean coercion — the STRING "false" must not read as truthy (bool("false") is
        # True), which would wrongly short-circuit the job to already_complete and close the issue.
        already_complete = _as_bool(data.get("already_complete")) and not tasks
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
        fmt_kwargs = {
            "task_title": task_title,
            "task_description": task_description,
            "acceptance_criteria": json.dumps(acceptance_criteria),
            "changes_summary": changes_summary or "(no changes recorded)",
            "revision_feedback": feedback_text,
        }
        try:
            data = call_llm_with_retries(
                lambda: _call_json(
                    self._adjudication_agent,
                    prompts.REVISION_ADJUDICATION_USER,
                    fmt_kwargs,
                    required_keys=("verdict",),
                    default={},
                    retries=False,
                ),
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
        """Groom one task: acceptance criteria, out of scope, enriched description, priority, subtasks.

        Preconditions:
            - ``task_id``/``task_title`` identify the task; ``task_dependencies`` is its
              already-known dependency id list; ``plan_context`` is the plan text this task was
              carved from.
        Postconditions:
            - Returns a dict with "acceptance_criteria", "out_of_scope", "description_enriched",
              "priority", "subtasks", and "task_dependencies". On any LLM/parse failure, falls back
              to the ungroomed defaults (``task_description`` verbatim, "medium" priority, no
              subtasks, ``task_dependencies`` unchanged) rather than raising.
        """
        data = _call_json(
            self._groom_agent,
            prompts.GROOM_TASK_USER,
            {
                "task_id": task_id,
                "task_title": task_title,
                "task_description": task_description,
                "task_dependencies": json.dumps(task_dependencies),
                "plan_context": plan_context,
            },
            required_keys=(
                "acceptance_criteria",
                "description_enriched",
                "subtasks",
                "out_of_scope",
            ),
            default={
                "acceptance_criteria": [],
                "out_of_scope": "",
                "description_enriched": task_description,
                "priority": "medium",
                "subtasks": [],
                "task_dependencies": task_dependencies,
            },
            label="groom_task",
        )
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
        """Suggest assignments: list of { agent_id, task_id }. Orchestrator calls Task Graph assign.

        Preconditions:
            - ``agent_ids`` is the full worker roster; ``ready_tasks`` are tasks with no outstanding
              dependencies; ``free_agents`` is the subset of ``agent_ids`` currently idle.
        Postconditions:
            - Returns ``{"assignments": [...]}`` where each entry is a dict carrying both a
              non-empty "agent_id" and "task_id"; any malformed or incomplete entry from the LLM
              response is dropped rather than surfaced.
        """
        data = _call_json(
            self._assignment_agent,
            prompts.ASSIGNMENT_USER,
            {
                "agent_ids": json.dumps(agent_ids),
                "ready_tasks": json.dumps(ready_tasks),
                "free_agents": json.dumps(free_agents),
            },
            required_keys=("assignments",),
            default={"assignments": []},
            label="assignments",
        )
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
        spec_content: str = "",
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
            - ``spec_content`` is the plan's full spec text (``CodingTeamPlanInput.final_spec_content``)
              or "". This is the swarm's sole code-review call (the quality gate's duplicate review
              was removed), so it is the only place global spec constraints outside a task's own
              description/acceptance criteria can be checked. Empty adds nothing to the prompt.

        Postconditions:
            - When ``progress_callback`` is provided, each LLM attempt and each
              backoff wait is reported, and a terminal ``done`` report at 1.0 is
              emitted on both the success and the exhausted-retries paths.
            - The review result is identical whether or not a callback is provided.
        """
        fmt_kwargs = {
            "task_title": task_title,
            "task_description": task_description,
            "acceptance_criteria": json.dumps(acceptance_criteria),
            "changes_summary": changes_summary,
        }
        extra = ""
        if spec_content.strip():
            # Appended (not a CODE_REVIEW_USER placeholder) so the template's .format() keys are
            # untouched, mirroring the user-decisions block below.
            extra += (
                "\n\nProject specification (check the change complies with any constraints here "
                "beyond the task's own description/acceptance criteria):\n" + spec_content
            )
        decisions = [str(d).strip() for d in (user_decisions or []) if str(d).strip()]
        if decisions:
            # Appended (not a CODE_REVIEW_USER placeholder) so the template's .format() keys are
            # untouched. Mirrors the planning path's "User decisions" block in _plan_text.
            extra += (
                "\n\nUser decisions already made (answered by the user — these are settled; "
                "do NOT request changes to revisit them or treat them as open/unanswered "
                "questions):\n" + "\n".join(f"- {d}" for d in decisions)
            )
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
        # Fresh, call-local review agent: this method runs concurrently across tasks in the
        # orchestrator's review fan-out, and a Strands Agent mutates its own conversation history on
        # every call. A per-call instance keeps each review's history isolated (no shared mutable
        # state across threads); it also means each review starts from a clean slate rather than
        # inheriting prior tasks' turns.
        review_agent = Agent(model=self._model, system_prompt=prompts.CODE_REVIEW_SYSTEM)

        def _attempt_review() -> Dict[str, Any]:
            nonlocal attempt_no
            attempt_no += 1
            _report(
                "reviewing",
                f"attempt {attempt_no}/{attempts}",
                min(0.1 + 0.8 * (attempt_no - 1) / attempts, 0.9),
            )
            data = _call_json(
                review_agent,
                prompts.CODE_REVIEW_USER,
                fmt_kwargs,
                required_keys=("approved",),
                default={},
                retries=False,
                extra=extra,
            )
            # A response that parses as JSON but carries no usable verdict is not a substantive
            # rejection — it's an unusable review. ``approved`` must be a real boolean: a missing or
            # null verdict, or a fabricated non-bool that tolerant repair completed from a truncated
            # ``{"approved": `` (which yields ``""``), must NOT slip through as ``approved=False`` and
            # burn the revision loop. Raise so the shared retry envelope retries and ultimately
            # surfaces error=True (fail once).
            if not isinstance(data.get("approved"), bool):
                raise ValueError(f"review response missing a boolean 'approved' verdict: {data!r}")
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
