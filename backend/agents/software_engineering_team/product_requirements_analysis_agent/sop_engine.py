"""
SOP (Standard Operating Procedure) discovery engine for the Product Requirements Analysis Agent.

The one-time pre-loop discovery that runs before the spec-review cycle: Phase 1
(``run_sop_phase1``) gathers environment-constraint decisions sub-phase by
sub-phase; Phase 2 (``run_sop_phase2_architecture``) analyzes and confirms the
target architecture. Related project context/constraints discovery lives in
:mod:`context_discovery`.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .context_discovery import inject_context_answers_into_spec
from .llm_io import call_llm_json
from .models import (
    AnsweredQuestion,
    ArchitectureAnalysisResult,
    OpenQuestion,
    QuestionOption,
    SOPDecision,
    SOPSubPhase,
    ToolGapAnalysis,
    ToolRecommendation,
)
from .prompts import (
    SOP_ARCHITECTURE_ANALYSIS_PROMPT,
    SOP_GENERATE_OPTIONS_PROMPT,
    SOP_SPEC_EXTRACTION_PROMPT,
    SOP_SUB_PHASE_GAP_ANALYSIS_PROMPT,
    SOP_SUB_PHASE_OBJECTIVES,
)
from .qa_history import record_answers
from .question_data import SOP_PHASE1_QUESTIONS
from .user_communication import communicate_with_user

logger = logging.getLogger(__name__)

MAX_SOP_ROUNDS = 5  # Safety limit for multi-round SOP Phase 1 (hardcoded questions)
MAX_GAP_ROUNDS = 3  # Safety limit for LLM gap-analysis follow-up rounds per sub-phase

# One-pass slot matcher for gap-analysis prompt fill. Values must never be
# rescanned (spec/decision text may literally contain later placeholder names).
_GAP_ANALYSIS_SLOT_RE = re.compile(
    r"\{("
    r"sub_phase_name|sub_phase_objective|spec_excerpt|"
    r"sub_phase_decisions|all_decisions|existing_question_ids"
    r")\}"
)


def _fill_gap_analysis_prompt(values: Dict[str, str]) -> str:
    """Substitute gap-analysis placeholders in a single non-rescanning pass.

    Preconditions: ``values`` contains every named slot in
        ``_GAP_ANALYSIS_SLOT_RE``; values are strings (may contain braces or
        literal ``{slot}`` tokens).
    Postconditions: returns the filled prompt; only placeholders present in the
        original template are replaced; replacement text is never re-matched.
    """
    required = {
        "sub_phase_name",
        "sub_phase_objective",
        "spec_excerpt",
        "sub_phase_decisions",
        "all_decisions",
        "existing_question_ids",
    }
    assert required <= set(values), f"missing gap-analysis slots: {required - set(values)}"
    return _GAP_ANALYSIS_SLOT_RE.sub(lambda m: values[m.group(1)], SOP_SUB_PHASE_GAP_ANALYSIS_PROMPT)


def evaluate_sop_conditionals(
    question_def: Dict[str, Any],
    decisions_map: Dict[str, str],
) -> Optional[bool]:
    """Evaluate whether a conditional SOP question should be asked.

    Returns:
        True  – question should be asked (condition met or no condition).
        False – question should NOT be asked (condition not met).
        None  – parent not yet answered; defer to a later round.

    Preconditions: ``question_def`` may carry a ``depends_on`` mapping of
        parent_id -> required values; ``decisions_map`` maps answered ids -> answers.
    Postconditions: pure; returns one of the three states above.
    """
    depends_on = question_def.get("depends_on")
    if not depends_on:
        return True  # No condition — always ask
    for parent_id, required_values in depends_on.items():
        if parent_id not in decisions_map:
            return None  # Parent not answered yet — defer
        parent_answer = decisions_map[parent_id]
        # Exact match (case-insensitive, whitespace-stripped) against the required
        # values — not a substring check, which previously caused false positives
        # (e.g. required value "Yes" matching a parent answer like "Yesterday").
        parent_lower = parent_answer.lower().strip()
        required_lower = {v.lower().strip() for v in required_values}
        if parent_lower not in required_lower:
            return False  # Condition not met
    return True


def extract_sop_decisions_from_spec(model: Any, spec_content: str) -> List[SOPDecision]:
    """Scan the spec for answers to SOP Phase 1 questions using an LLM call.

    Returns SOPDecision objects with source='spec' for questions clearly answered by
    the specification. Returns an empty list on LLM failure.

    Preconditions: ``model`` is a Strands ``Model``.
    Postconditions: only decisions with confidence >= 0.7 for known sop_ids are
        returned; never raises.
    """
    if not spec_content or not spec_content.strip():
        return []

    # Build a compact JSON summary of all SOP questions for the prompt
    questions_summary = []
    for sub_phase, q_defs in SOP_PHASE1_QUESTIONS.items():
        for q_def in q_defs:
            option_labels = [o["label"] for o in q_def.get("options", []) if "label" in o]
            questions_summary.append(
                {
                    "sop_id": q_def["sop_id"],
                    "sub_phase": sub_phase.value,
                    "question": q_def["question_text"],
                    "options": option_labels,
                }
            )

    prompt = SOP_SPEC_EXTRACTION_PROMPT.format(
        sop_questions_json=json.dumps(questions_summary, indent=2),
        spec_content=spec_content,
    )

    try:
        parsed = call_llm_json(model, prompt)
        if not isinstance(parsed, dict):
            return []
        extracted = parsed.get("extracted_decisions", [])
        if not isinstance(extracted, list):
            return []

        decisions: List[SOPDecision] = []
        # Build a reverse lookup for sop_id -> sub_phase
        id_to_sub_phase: Dict[str, SOPSubPhase] = {}
        id_to_question: Dict[str, str] = {}
        for sp, q_defs in SOP_PHASE1_QUESTIONS.items():
            for q_def in q_defs:
                id_to_sub_phase[q_def["sop_id"]] = sp
                id_to_question[q_def["sop_id"]] = q_def["question_text"]

        for item in extracted:
            sop_id = str(item.get("sop_id", ""))
            if sop_id not in id_to_sub_phase:
                continue
            confidence = float(item.get("confidence", 0.0))
            if confidence < 0.7:
                continue  # Skip low-confidence extractions
            decisions.append(
                SOPDecision(
                    sop_id=sop_id,
                    sub_phase=id_to_sub_phase[sop_id],
                    question_text=id_to_question.get(sop_id, ""),
                    decision=str(item.get("decision", "")),
                    source="spec",
                    confidence=confidence,
                )
            )
        return decisions
    except Exception as exc:
        logger.warning("SOP spec extraction failed, will ask all questions: %s", str(exc))
        return []


def generate_spec_aware_options(
    model: Any,
    q_def: Dict[str, Any],
    spec_content: str,
    decisions_map: Dict[str, str],
) -> List[QuestionOption]:
    """Generate answer options for a question using the LLM, informed by the spec and prior decisions.

    Returns a list of QuestionOption objects. Falls back to an empty list on failure.

    Preconditions: ``model`` is a Strands ``Model``; ``q_def`` carries ``sop_id`` and
        ``question_text``.
    Postconditions: returns parsed options (possibly empty); never raises.
    """
    prior_decisions_str = json.dumps(decisions_map, indent=2) if decisions_map else "{}"
    prompt = SOP_GENERATE_OPTIONS_PROMPT.format(
        question_text=q_def["question_text"],
        sop_id=q_def["sop_id"],
        prior_decisions=prior_decisions_str,
        spec_excerpt=spec_content,
    )
    try:
        parsed = call_llm_json(model, prompt)
        if not isinstance(parsed, dict):
            return []
        raw_options = parsed.get("options", [])
        if not isinstance(raw_options, list):
            return []
        options: List[QuestionOption] = []
        for i, opt in enumerate(raw_options):
            if not isinstance(opt, dict) or "label" not in opt:
                continue
            options.append(
                QuestionOption(
                    id=opt.get("id", f"opt_gen_{i}"),
                    label=opt["label"],
                    is_default=bool(opt.get("is_default", False)),
                    rationale=str(opt.get("rationale", "")),
                    confidence=float(opt.get("confidence", 0.5)),
                )
            )
        return options
    except Exception as exc:
        logger.warning("Spec-aware option generation failed for %s: %s", q_def["sop_id"], str(exc))
        return []


def _pad_to_minimum_options(
    options: List[QuestionOption],
    min_options: int,
    *,
    text_default: bool,
) -> List[QuestionOption]:
    """Ensure at least ``min_options`` options by appending 'Other' and, if still
    short, inserting a free-text placeholder at the front.

    Shared by :func:`build_question_options` and :func:`assess_sub_phase_gaps` so
    the option-padding policy lives in one place.

    Preconditions: ``options`` is a list of :class:`QuestionOption`; ``min_options``
        is a non-negative int.
    Postconditions: returns a new list, leaving ``options`` unmutated; an existing
        "Other" option (case-insensitive label match) is not duplicated; returns
        ``options`` unchanged (by value) if it already has >= ``min_options``
        entries.
    """
    if len(options) >= min_options:
        return list(options)
    padded = list(options)
    if not any(o.label.lower() == "other" for o in padded):
        padded.append(
            QuestionOption(
                id="opt_other",
                label="Other",
                is_default=False,
                rationale="Specify your preference.",
                confidence=0.3,
            )
        )
    if len(padded) < min_options and not any(
        o.label.lower() == "(please type your answer)" for o in padded
    ):
        padded.insert(
            0,
            QuestionOption(
                id="opt_text",
                label="(Please type your answer)",
                is_default=text_default,
                rationale="",
                confidence=0.5,
            ),
        )
    return padded


def _ensure_single_default(options: List[QuestionOption]) -> List[QuestionOption]:
    """Return a copy of ``options`` with exactly one ``is_default=True`` entry.

    Shared by :func:`build_question_options` and :func:`assess_sub_phase_gaps`,
    both of which combine hardcoded/LLM-generated/padded options that may each
    carry their own default flag.

    Preconditions: ``options`` is a list of :class:`QuestionOption`.
    Postconditions: returns a new list, leaving ``options`` unmutated; if no
        option was default, the first becomes default; if more than one was,
        only the first of those remains default. No-op (returns ``[]``) on an
        empty list.
    """
    if not options:
        return []
    default_index = next((i for i, o in enumerate(options) if o.is_default), 0)
    return [
        o
        if o.is_default == (i == default_index)
        else o.model_copy(update={"is_default": i == default_index})
        for i, o in enumerate(options)
    ]


def build_question_options(
    model: Any,
    q_def: Dict[str, Any],
    spec_content: str,
    decisions_map: Dict[str, str],
) -> List[QuestionOption]:
    """Build options for a question, ensuring at least 3 valid options.

    Uses hardcoded options if >= 3 are available. Otherwise generates spec-aware
    options via LLM and merges them with any existing hardcoded options.

    Preconditions: ``model`` is a Strands ``Model``; ``q_def`` may carry ``options``.
    Postconditions: returns >= 3 options whenever possible (padding with an "Other"
        and/or free-text option); never raises.
    """
    MIN_OPTIONS = 3

    # Start with hardcoded options
    hardcoded = q_def.get("options", [])
    options: List[QuestionOption] = []
    for i, opt in enumerate(hardcoded):
        options.append(
            QuestionOption(
                id=opt.get("id", f"opt{i}"),
                label=opt["label"],
                is_default=opt.get("is_default", False),
                rationale=opt.get("rationale", ""),
                confidence=0.5,
            )
        )

    if len(options) >= MIN_OPTIONS:
        return options

    # Not enough options — generate spec-aware options via LLM
    generated = generate_spec_aware_options(model, q_def, spec_content, decisions_map)
    existing_labels = {o.label.lower() for o in options}
    for gen_opt in generated:
        if gen_opt.label.lower() not in existing_labels:
            options.append(gen_opt)
            existing_labels.add(gen_opt.label.lower())

    options = _pad_to_minimum_options(options, MIN_OPTIONS, text_default=True)
    return _ensure_single_default(options)


def assess_sub_phase_gaps(
    model: Any,
    sub_phase: SOPSubPhase,
    spec_content: str,
    all_decisions: List[SOPDecision],
    decisions_map: Dict[str, str],
) -> Tuple[bool, List[OpenQuestion]]:
    """Assess whether a sub-phase is complete and generate follow-up questions for gaps.

    Uses an LLM call to evaluate the sub-phase against its objectives and the
    information collected so far (from spec + user answers). If gaps remain, the LLM
    generates targeted follow-up questions with spec-aware options.

    On LLM error or malformed response, returns ``(True, [])`` to gracefully degrade
    and avoid blocking the workflow.

    Returns (is_complete, follow_up_questions).

    Preconditions: ``model`` is a Strands ``Model``; ``sub_phase`` is a valid enum.
    Postconditions: follow-up questions have >= 3 options and exactly one default;
        duplicate IDs (already in ``decisions_map``) are skipped; never raises.
    """
    objective = SOP_SUB_PHASE_OBJECTIVES.get(sub_phase.value, "")
    if not objective:
        return True, []

    # Collect decisions for this sub-phase only
    sub_phase_decisions = [
        {
            "sop_id": d.sop_id,
            "question": d.question_text,
            "decision": d.decision,
            "source": d.source,
        }
        for d in all_decisions
        if d.sub_phase == sub_phase
    ]
    # Also include all decisions for cross-referencing
    all_decisions_summary = [
        {
            "sop_id": d.sop_id,
            "sub_phase": d.sub_phase.value
            if isinstance(d.sub_phase, SOPSubPhase)
            else str(d.sub_phase),
            "decision": d.decision,
        }
        for d in all_decisions
    ]
    # Build list of existing question IDs so the LLM avoids regenerating them
    existing_ids_str = ", ".join(sorted(decisions_map.keys())) if decisions_map else "(none)"

    # One-pass substitution: values may contain braces or literal ``{slot}``
    # tokens that must not be re-matched after insertion.
    prompt = _fill_gap_analysis_prompt(
        {
            "sub_phase_name": sub_phase.value,
            "sub_phase_objective": objective,
            "spec_excerpt": spec_content,
            "sub_phase_decisions": json.dumps(sub_phase_decisions, indent=2),
            "all_decisions": json.dumps(all_decisions_summary, indent=2),
            "existing_question_ids": existing_ids_str,
        }
    )

    try:
        parsed = call_llm_json(model, prompt)
        if not isinstance(parsed, dict):
            return True, []  # On failure, consider complete to avoid blocking

        is_complete = bool(parsed.get("is_complete", True))
        if is_complete:
            logger.info(
                "SOP Phase 1: Sub-phase '%s' assessed as COMPLETE: %s",
                sub_phase.value,
                str(parsed.get("completeness_rationale", "")),
            )
            return True, []

        logger.info(
            "SOP Phase 1: Sub-phase '%s' has GAPS: %s",
            sub_phase.value,
            str(parsed.get("completeness_rationale", "")),
        )

        # Parse follow-up questions
        raw_questions = parsed.get("follow_up_questions", [])
        if not isinstance(raw_questions, list):
            return False, []

        skipped_dupes = 0
        follow_ups: List[OpenQuestion] = []
        for rq in raw_questions:
            if not isinstance(rq, dict) or "question_text" not in rq:
                continue
            q_id = rq.get("id", f"P1.{sub_phase.value[:6]}.gen_{len(follow_ups) + 1}")
            # Skip if we already have a decision for this question ID
            if q_id in decisions_map:
                skipped_dupes += 1
                continue

            # Parse options from LLM response
            raw_opts = rq.get("options", [])
            options: List[QuestionOption] = []
            for i, opt in enumerate(raw_opts):
                if not isinstance(opt, dict) or "label" not in opt:
                    continue
                options.append(
                    QuestionOption(
                        id=opt.get("id", f"opt_gen_{i}"),
                        label=opt["label"],
                        is_default=bool(opt.get("is_default", False)),
                        rationale=str(opt.get("rationale", "")),
                        confidence=float(opt.get("confidence", 0.5)),
                    )
                )

            # Ensure minimum 3 options, exactly one of them the default
            options = _pad_to_minimum_options(options, 3, text_default=False)
            options = _ensure_single_default(options)

            follow_ups.append(
                OpenQuestion(
                    id=q_id,
                    question_text=rq["question_text"],
                    context=str(rq.get("context", "")),
                    category=str(rq.get("category", "general")),
                    priority=str(rq.get("priority", "high")),
                    allow_multiple=bool(rq.get("allow_multiple", False)),
                    source="sop_phase1",
                    sop_sub_phase=sub_phase.value,
                    options=options,
                )
            )

        if skipped_dupes:
            logger.warning(
                "SOP Phase 1: Sub-phase '%s' gap analysis generated %d question(s) with duplicate IDs — skipped",
                sub_phase.value,
                skipped_dupes,
            )

        return False, follow_ups
    except Exception as exc:
        logger.error("Sub-phase gap analysis failed for '%s': %s", sub_phase.value, str(exc))
        return True, []  # On failure, consider complete to avoid blocking


def _record_sop_answers(
    sub_phase: SOPSubPhase,
    answered: List[AnsweredQuestion],
    repo_path: Path,
    decisions_map: Dict[str, str],
    all_decisions: List[SOPDecision],
    all_answered: List[AnsweredQuestion],
) -> None:
    """Record a round's answers as SOPDecisions and persist them to qa_history.

    Shared by :func:`_run_sop_sub_phase`'s hardcoded-question and gap-analysis
    rounds, which otherwise duplicated this exact bookkeeping.

    Preconditions: ``answered`` is a non-empty list of :class:`AnsweredQuestion`.
    Postconditions: mutates ``decisions_map``/``all_decisions``/``all_answered`` in
        place (they are the caller's own local working copies) and appends to
        qa_history via :func:`record_answers`.
    """
    for aq in answered:
        decision = SOPDecision(
            sop_id=aq.question_id,
            sub_phase=sub_phase,
            question_text=aq.question_text,
            decision=aq.selected_answer,
            source="user",
            confidence=1.0,
        )
        all_decisions.append(decision)
        decisions_map[aq.question_id] = aq.selected_answer

    all_answered.extend(answered)
    record_answers(repo_path, answered, iteration=0)


def _run_sop_sub_phase(
    model: Any,
    spec_content: str,
    repo_path: Path,
    job_id: str,
    job_updater: Callable,
    sub_phase: SOPSubPhase,
    decisions_map: Dict[str, str],
    all_decisions: List[SOPDecision],
    all_answered: List[AnsweredQuestion],
) -> Tuple[Dict[str, str], List[SOPDecision], List[AnsweredQuestion]]:
    """Run one SOP Phase 1 sub-phase: hardcoded-question rounds, then gap-analysis rounds.

    Extracted from :func:`run_sop_phase1` to keep that function a thin per-sub-phase
    orchestrator.

    Preconditions: ``model`` is a Strands ``Model``; ``job_id`` identifies a live
        job; ``decisions_map``/``all_decisions``/``all_answered`` carry state
        accumulated from any prior sub-phases (for cross-referencing).
    Postconditions: returns this sub-phase's updated (decisions_map, all_decisions,
        all_answered); the caller's input collections are not mutated. Honors
        ``MAX_SOP_ROUNDS``/``MAX_GAP_ROUNDS``; propagates communication failures.
    """
    decisions_map = dict(decisions_map)
    all_decisions = list(all_decisions)
    all_answered = list(all_answered)

    q_defs = SOP_PHASE1_QUESTIONS.get(sub_phase, [])

    # --- Phase A: Ask hardcoded SOP questions (including conditional follow-ups) ---
    for round_num in range(1, MAX_SOP_ROUNDS + 1):
        sub_phase_questions: List[OpenQuestion] = []

        for q_def in q_defs:
            sop_id = q_def["sop_id"]
            if sop_id in decisions_map:
                continue  # Already answered (from spec or prior round)

            cond_result = evaluate_sop_conditionals(q_def, decisions_map)
            if cond_result is False:
                continue  # Condition not met
            if cond_result is None:
                continue  # Parent not answered yet — defer to next round within this sub-phase

            # Build options ensuring at least 3 valid choices, informed by spec
            options = build_question_options(model, q_def, spec_content, decisions_map)

            sub_phase_questions.append(
                OpenQuestion(
                    id=sop_id,
                    question_text=q_def["question_text"],
                    context="",
                    category=q_def.get("category", "general"),
                    priority="high",
                    allow_multiple=q_def.get("allow_multiple", False),
                    source="sop_phase1",
                    sop_sub_phase=sub_phase.value,
                    options=options,
                )
            )

        if not sub_phase_questions:
            break  # No more hardcoded questions for this sub-phase

        logger.info(
            "SOP Phase 1 sub-phase '%s' round %d: asking %d questions",
            sub_phase.value,
            round_num,
            len(sub_phase_questions),
        )
        job_updater(
            status_text=f"SOP Phase 1 — {sub_phase.value}: waiting for answers to {len(sub_phase_questions)} question(s)",
        )

        try:
            answered = communicate_with_user(
                job_id=job_id,
                open_questions=sub_phase_questions,
                repo_path=repo_path,
                iteration=0,
            )
        except Exception as exc:
            logger.error(
                "SOP Phase 1 communication failed in sub-phase '%s': %s",
                sub_phase.value,
                exc,
            )
            raise

        if not answered:
            logger.info(
                "SOP Phase 1: No answers received for sub-phase '%s' round %d",
                sub_phase.value,
                round_num,
            )
            break

        _record_sop_answers(
            sub_phase, answered, repo_path, decisions_map, all_decisions, all_answered
        )
    else:
        logger.warning(
            "SOP Phase 1: sub-phase '%s' hit MAX_SOP_ROUNDS (%d) — some hardcoded "
            "questions may remain unanswered",
            sub_phase.value,
            MAX_SOP_ROUNDS,
        )

    # --- Phase B: Gap analysis — generate follow-up questions until sub-phase is complete ---
    for gap_round in range(1, MAX_GAP_ROUNDS + 1):
        job_updater(
            status_text=f"SOP Phase 1 — {sub_phase.value}: assessing completeness...",
        )
        is_complete, follow_ups = assess_sub_phase_gaps(
            model,
            sub_phase,
            spec_content,
            all_decisions,
            decisions_map,
        )
        if is_complete or not follow_ups:
            logger.info(
                "SOP Phase 1: Sub-phase '%s' is complete after %d gap-analysis round(s)",
                sub_phase.value,
                gap_round,
            )
            break

        logger.info(
            "SOP Phase 1 sub-phase '%s' gap round %d: asking %d follow-up questions",
            sub_phase.value,
            gap_round,
            len(follow_ups),
        )
        job_updater(
            status_text=f"SOP Phase 1 — {sub_phase.value}: {len(follow_ups)} follow-up question(s) to fill gaps",
        )

        try:
            answered = communicate_with_user(
                job_id=job_id,
                open_questions=follow_ups,
                repo_path=repo_path,
                iteration=0,
            )
        except Exception as exc:
            logger.error(
                "SOP Phase 1 gap-analysis communication failed in sub-phase '%s': %s",
                sub_phase.value,
                exc,
            )
            raise

        if not answered:
            logger.info(
                "SOP Phase 1: No answers to gap questions for sub-phase '%s'",
                sub_phase.value,
            )
            break

        _record_sop_answers(
            sub_phase, answered, repo_path, decisions_map, all_decisions, all_answered
        )
    else:
        logger.warning(
            "SOP Phase 1: sub-phase '%s' hit MAX_GAP_ROUNDS (%d) — gap analysis may "
            "not have converged",
            sub_phase.value,
            MAX_GAP_ROUNDS,
        )

    return decisions_map, all_decisions, all_answered


def run_sop_phase1(
    model: Any,
    spec_content: str,
    repo_path: Path,
    job_id: str,
    job_updater: Callable,
) -> Tuple[List[SOPDecision], str, List[AnsweredQuestion]]:
    """Run SOP Phase 1: Environment Constraints & Requirements.

    Sequential sub-phase approach:
    1. Extract answers already present in the spec.
    2. Iterate through each sub-phase one at a time (DEPLOYMENT, REGULATIONS, ..., PRIORITIES),
       via :func:`_run_sop_sub_phase`: first ask the hardcoded SOP questions (with
       conditional follow-ups), then assess whether the sub-phase is complete using
       LLM gap analysis, asking follow-up questions until it is. Every question is
       guaranteed at least 3 answer options informed by the spec.
    3. Inject all collected decisions into the spec as a context section.

    Returns (all_decisions, updated_spec, answered_questions).

    Preconditions: ``model`` is a Strands ``Model``; ``job_id`` identifies a live job.
    Postconditions: all user answers are recorded as decisions and to qa_history; the
        returned spec has a project-context section injected when any answers were
        collected. Propagates communication failures.
    """
    # Step 1: Extract decisions from spec (single upfront call for efficiency)
    spec_decisions = extract_sop_decisions_from_spec(model, spec_content)
    decisions_map: Dict[str, str] = {d.sop_id: d.decision for d in spec_decisions}
    all_decisions = list(spec_decisions)
    all_answered: List[AnsweredQuestion] = []

    if spec_decisions:
        logger.info(
            "SOP Phase 1: Extracted %d decisions from spec: %s",
            len(spec_decisions),
            [d.sop_id for d in spec_decisions],
        )

    # Step 2: Iterate through sub-phases ONE AT A TIME in order
    for sub_phase in SOPSubPhase:
        decisions_map, all_decisions, all_answered = _run_sop_sub_phase(
            model,
            spec_content,
            repo_path,
            job_id,
            job_updater,
            sub_phase,
            decisions_map,
            all_decisions,
            all_answered,
        )

    # Step 3: Inject all decisions into spec
    if all_answered:
        spec_content = inject_context_answers_into_spec(spec_content, all_answered)

    return all_decisions, spec_content, all_answered


def run_sop_phase2_architecture(
    model: Any,
    spec_content: str,
    sop_decisions: List[SOPDecision],
    repo_path: Path,
    job_id: str,
    job_updater: Callable,
) -> Tuple[ArchitectureAnalysisResult, str]:
    """Run SOP Phase 2: Architecture Analysis.

    Autonomously analyzes architecture based on spec + Phase 1 decisions, then
    presents results for user approval.

    Returns (architecture_result, updated_spec).

    Preconditions: ``model`` is a Strands ``Model``; ``job_id`` identifies a live job.
    Postconditions: writes ``architecture_analysis.md`` and prepends an architecture
        summary to the spec when analysis produced one; degrades gracefully on LLM or
        communication failure.
    """
    arch_result = ArchitectureAnalysisResult()

    # Format Phase 1 decisions for the prompt
    decisions_json = json.dumps(
        [
            {
                "sop_id": d.sop_id,
                "sub_phase": d.sub_phase.value
                if isinstance(d.sub_phase, SOPSubPhase)
                else str(d.sub_phase),
                "question": d.question_text,
                "decision": d.decision,
                "source": d.source,
            }
            for d in sop_decisions
        ],
        indent=2,
    )

    # Step 1: Architecture analysis LLM call
    job_updater(status_text="Analyzing architecture based on requirements...")
    prompt = SOP_ARCHITECTURE_ANALYSIS_PROMPT.format(
        spec_content=spec_content,
        phase1_decisions_json=decisions_json,
    )

    try:
        parsed = call_llm_json(model, prompt)
        if isinstance(parsed, dict):
            arch_result = ArchitectureAnalysisResult(
                architecture_type=str(parsed.get("architecture_type", "")),
                architecture_rationale=str(parsed.get("architecture_rationale", "")),
                data_types_and_storage=parsed.get("data_types_and_storage", []),
                task_types=parsed.get("task_types", []),
                tool_gaps=[
                    ToolGapAnalysis(
                        gap_description=g.get("gap_description", ""),
                        recommendations=[
                            ToolRecommendation(
                                name=r.get("name", ""),
                                description=r.get("description", ""),
                                why_recommended=r.get("why_recommended", ""),
                            )
                            for r in g.get("recommendations", [])
                        ],
                    )
                    for g in parsed.get("tool_gaps", [])
                ],
                diagrams=parsed.get("diagrams", {}),
                summary=str(parsed.get("summary", "")),
            )
    except Exception as exc:
        logger.warning("SOP Phase 2 architecture analysis failed: %s", str(exc))

    # Step 2: Generate approval questions
    if arch_result.architecture_type or arch_result.tool_gaps:
        job_updater(status_text="Preparing architecture recommendations for approval...")
        approval_questions = build_architecture_approval_questions(arch_result)

        if approval_questions:
            try:
                answered = communicate_with_user(
                    job_id=job_id,
                    open_questions=approval_questions,
                    repo_path=repo_path,
                    iteration=0,
                )
                if answered:
                    apply_architecture_approval(arch_result, answered)
                    record_answers(repo_path, answered, iteration=0)
            except Exception as exc:
                logger.warning("SOP Phase 2 approval communication failed: %s", str(exc))

    # Step 3: Save architecture document
    product_analysis_dir = repo_path / "plan" / "product_analysis"
    product_analysis_dir.mkdir(parents=True, exist_ok=True)
    arch_doc_path = product_analysis_dir / "architecture_analysis.md"
    try:
        arch_doc_content = format_architecture_document(arch_result)
        arch_doc_path.write_text(arch_doc_content, encoding="utf-8")
        logger.info("Saved architecture analysis to %s", arch_doc_path)
    except OSError as exc:
        logger.warning("Failed to save architecture document: %s", exc)

    # Step 4: Inject architecture summary into spec
    if arch_result.summary:
        arch_section = "\n\n## Architecture Analysis\n\n"
        arch_section += arch_result.summary + "\n"
        if arch_result.architecture_type:
            arch_section += f"\n**Architecture Type:** {arch_result.architecture_type}\n"
        spec_content = arch_section + "\n---\n\n" + spec_content

    return arch_result, spec_content


def build_architecture_approval_questions(
    arch_result: ArchitectureAnalysisResult,
) -> List[OpenQuestion]:
    """Build approval questions from architecture analysis results.

    Preconditions: ``arch_result`` is an :class:`ArchitectureAnalysisResult`.
    Postconditions: returns an architecture-type approval question (when a type was
        recommended) plus one selection question per tool gap with >1 recommendation.
    """
    questions: List[OpenQuestion] = []

    # Architecture type approval
    if arch_result.architecture_type:
        options = [
            QuestionOption(
                id="opt_approve",
                label=f"Approve {arch_result.architecture_type} architecture",
                is_default=True,
                rationale=arch_result.architecture_rationale
                if arch_result.architecture_rationale
                else "",
                confidence=0.8,
            ),
            QuestionOption(
                id="opt_modify",
                label="Suggest a different architecture",
                is_default=False,
                rationale="If the recommended architecture doesn't fit your needs.",
                confidence=0.2,
            ),
        ]
        questions.append(
            OpenQuestion(
                id="arch_type_approval",
                question_text=f"We recommend a {arch_result.architecture_type} architecture. Do you approve?",
                context=arch_result.architecture_rationale
                if arch_result.architecture_rationale
                else "",
                category="architecture",
                priority="high",
                options=options,
                source="sop_phase2",
            )
        )

    # Gap tool selection questions
    for i, gap in enumerate(arch_result.tool_gaps):
        if len(gap.recommendations) <= 1:
            continue  # No choice needed
        options = []
        for j, rec in enumerate(gap.recommendations):
            options.append(
                QuestionOption(
                    id=f"opt_gap{i}_{j}",
                    label=rec.name,
                    is_default=j == 0,
                    rationale=rec.why_recommended or rec.description,
                    confidence=0.7 if j == 0 else 0.4,
                )
            )
        questions.append(
            OpenQuestion(
                id=f"gap_{i}_selection",
                question_text=f"Which tool do you prefer for: {gap.gap_description}?",
                context=gap.gap_description,
                category="infrastructure",
                priority="medium",
                options=options,
                source="sop_phase2",
            )
        )

    return questions


def apply_architecture_approval(
    arch_result: ArchitectureAnalysisResult,
    answered: List[AnsweredQuestion],
) -> None:
    """Apply user approval answers to the architecture result.

    Preconditions: ``arch_result`` and ``answered`` correspond to the same run.
    Postconditions: mutates ``arch_result`` in place — overriding the architecture
        type when the user typed an alternative, and recording each gap's selected
        recommendation; never raises.
    """
    for aq in answered:
        if aq.question_id == "arch_type_approval" and "different" in aq.selected_answer.lower():
            # User wants a different architecture — note it but keep original as reference
            if aq.other_text:
                arch_result.architecture_type = aq.other_text
        elif aq.question_id.startswith("gap_") and aq.question_id.endswith("_selection"):
            # Extract gap index from question_id like "gap_0_selection"
            try:
                gap_idx = int(aq.question_id.split("_")[1])
                if 0 <= gap_idx < len(arch_result.tool_gaps):
                    arch_result.tool_gaps[gap_idx].selected_recommendation = aq.selected_answer
            except (ValueError, IndexError):
                pass


def format_architecture_document(arch_result: ArchitectureAnalysisResult) -> str:
    """Format architecture analysis result as a Markdown document.

    Preconditions: ``arch_result`` is an :class:`ArchitectureAnalysisResult`.
    Postconditions: returns a Markdown document; sections are omitted when their
        corresponding fields are empty; never raises.
    """
    lines = ["# Architecture Analysis\n"]

    if arch_result.architecture_type:
        lines.append(f"## Architecture Type: {arch_result.architecture_type}\n")
        if arch_result.architecture_rationale:
            lines.append(arch_result.architecture_rationale + "\n")

    if arch_result.data_types_and_storage:
        lines.append("\n## Data Types and Storage\n")
        for item in arch_result.data_types_and_storage:
            dt = item.get("data_type", "Unknown")
            store = item.get("recommended_store", "TBD")
            rat = item.get("rationale", "")
            lines.append(f"- **{dt}** → {store}" + (f" — {rat}" if rat else "") + "\n")

    if arch_result.task_types:
        lines.append("\n## Task Types\n")
        for item in arch_result.task_types:
            task = item.get("task", "Unknown")
            cls = item.get("classification", "")
            needs = item.get("compute_needs", "")
            lines.append(f"- **{task}**: {cls}" + (f" ({needs})" if needs else "") + "\n")

    if arch_result.tool_gaps:
        lines.append("\n## Gap Analysis\n")
        for gap in arch_result.tool_gaps:
            lines.append(f"\n### {gap.gap_description}\n")
            if gap.selected_recommendation:
                lines.append(f"**Selected:** {gap.selected_recommendation}\n")
            for rec in gap.recommendations:
                marker = " ✓" if rec.name == gap.selected_recommendation else ""
                lines.append(f"- **{rec.name}**{marker}: {rec.description}")
                if rec.why_recommended:
                    lines.append(f"  — {rec.why_recommended}")
                lines.append("\n")

    if arch_result.diagrams:
        lines.append("\n## Architecture Diagrams\n")
        for name, content in arch_result.diagrams.items():
            lines.append(f"\n### {name}\n")
            lines.append(content + "\n")

    if arch_result.summary:
        lines.append("\n## Summary\n")
        lines.append(arch_result.summary + "\n")

    return "\n".join(lines)
