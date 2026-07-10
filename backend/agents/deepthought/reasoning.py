"""Pure, deterministic reasoning helpers for the Deepthought team.

Single source of truth for the tree-shaping logic that both the thread-mode
runtime (:class:`deepthought.agent.DeepthoughtAgent` /
:class:`deepthought.orchestrator.DeepthoughtOrchestrator`) and the Temporal
workflow (:mod:`deepthought.temporal.workflows`) depend on. Keeping it here means
the deterministic Temporal workflow reuses *exactly* the same rules as the
in-process runtime instead of re-implementing them.

Design constraint (module invariant): every function here is PURE and
deterministic — no threading, no I/O, no LLM calls, and no ``uuid`` / ``time`` /
``random`` at module or call scope. UUID minting is injected via an
``id_factory`` callable so the Temporal workflow can pass ``workflow.uuid4`` and
the thread path can pass ``uuid.uuid4``. This is what makes the module safe to
import and call inside a Temporal workflow sandbox.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from deepthought.models import (
    AgentResult,
    AgentSpec,
    KnowledgeEntry,
    QueryAnalysis,
    SkillRequirement,
)

logger = logging.getLogger(__name__)

# Maximum child agents any single node may spawn.
MAX_CHILDREN_PER_AGENT = 5

# Global budget — shared across the whole tree. Thread mode enforces it via the
# orchestrator's spawn callback; Temporal mode enforces the same cap as
# deterministic workflow state.
DEFAULT_AGENT_BUDGET = 50

# Max chars to include per child answer in deliberation/synthesis (token control).
MAX_CHARS_PER_CHILD_ANSWER = 3000

# Similarity threshold for fuzzy question matching (0-1). Two focus questions
# whose normalised word overlap is at/above this are considered duplicates.
SIMILARITY_THRESHOLD = 0.70

# Max chars of an answer stored as a knowledge-base finding.
_MAX_FINDING_CHARS = 500


# ---------------------------------------------------------------------------
# Similarity / knowledge-base dedup
# ---------------------------------------------------------------------------


def normalise_words(text: str) -> set[str]:
    """Cheap bag-of-words normalisation for similarity checks.

    Postconditions:
        - Returns the set of lowercased words longer than two characters, with
          surrounding punctuation stripped.
    """
    return {w.lower().strip("?.,!;:") for w in text.split() if len(w) > 2}


def jaccard_similarity(a: str, b: str) -> float:
    """Jaccard similarity between two strings' normalised word sets.

    Postconditions:
        - Returns a value in ``[0.0, 1.0]``; ``0.0`` when either side has no
          usable words.
    """
    sa, sb = normalise_words(a), normalise_words(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def find_similar_entries(
    entries: list[KnowledgeEntry],
    question: str,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[KnowledgeEntry]:
    """Return the entries whose ``focus_question`` is similar to *question*.

    Preconditions:
        - ``0.0 <= threshold <= 1.0``.
    Postconditions:
        - Returns entries in input order whose similarity is at/above
          ``threshold`` (no mutation of ``entries``).
    """
    return [e for e in entries if jaccard_similarity(e.focus_question, question) >= threshold]


# ---------------------------------------------------------------------------
# Analysis parsing
# ---------------------------------------------------------------------------


def parse_analysis(data: dict[str, Any], focus_question: str) -> QueryAnalysis:
    """Parse raw LLM JSON into a :class:`QueryAnalysis`, with defensive defaults.

    Preconditions:
        - ``data`` is a mapping (possibly empty / partially populated).
    Postconditions:
        - Returns a valid ``QueryAnalysis``. Malformed skill entries are dropped,
          and an agent that reports it cannot answer yet supplies no skills is
          coerced to a direct answer (there is nothing to decompose into).
    """
    skills_raw = data.get("skill_requirements") or []
    skills: list[SkillRequirement] = []
    for s in skills_raw[:MAX_CHILDREN_PER_AGENT]:
        try:
            skills.append(SkillRequirement(**s))
        except Exception:
            logger.warning("Skipping malformed skill requirement: %s", s)

    can_answer = bool(data.get("can_answer_directly", False))
    # If the LLM says it can't answer but provides no skills, force direct.
    if not can_answer and not skills:
        can_answer = True

    return QueryAnalysis(
        summary=data.get("summary", focus_question),
        can_answer_directly=can_answer,
        direct_answer=data.get("direct_answer") if can_answer else None,
        confidence=float(data.get("confidence", 0.5)) if can_answer else 0.0,
        skill_requirements=[] if can_answer else skills,
    )


# ---------------------------------------------------------------------------
# Child spawning
# ---------------------------------------------------------------------------


def build_child_specs(
    skills: list[SkillRequirement],
    parent_spec: AgentSpec,
    id_factory: Callable[[], Any],
) -> list[AgentSpec]:
    """Create :class:`AgentSpec` objects for each required specialist.

    ``id_factory`` mints a fresh unique id per child — thread mode passes
    ``uuid.uuid4``; the Temporal workflow passes ``workflow.uuid4`` so ids are
    deterministic across replay.

    Preconditions:
        - ``id_factory`` returns a value coercible to ``str`` on each call.
    Postconditions:
        - Returns at most ``MAX_CHILDREN_PER_AGENT`` specs, each one level deeper
          than ``parent_spec`` and pointing back at it via ``parent_id``.
    """
    specs: list[AgentSpec] = []
    for skill in skills[:MAX_CHILDREN_PER_AGENT]:
        specs.append(
            AgentSpec(
                agent_id=str(id_factory()),
                name=skill.name,
                role_description=skill.description,
                focus_question=skill.focus_question,
                depth=parent_spec.depth + 1,
                parent_id=parent_spec.agent_id,
            )
        )
    return specs


def results_to_dicts(child_results: list[AgentResult]) -> list[dict]:
    """Project child results to the compact dicts used in prompt formatting."""
    return [
        {
            "agent_name": r.agent_name,
            "focus_question": r.focus_question,
            "confidence": r.confidence,
            "answer": r.answer,
        }
        for r in child_results
    ]


# ---------------------------------------------------------------------------
# Structural confidence
# ---------------------------------------------------------------------------


def compute_structural_confidence(
    *,
    was_decomposed: bool,
    self_assessed: float,
    child_results: list[AgentResult],
    deliberation_notes: str = "",
) -> float:
    """Derive confidence from structural signals rather than LLM self-assessment.

    Signals used:
    - Direct answers get a modest base (the LLM self-assessment is just one signal).
    - Decomposed answers: weighted by child agreement and coverage.
    - Penalty for contradictions found in deliberation.
    - Bonus for multiple children agreeing (convergence).

    Postconditions:
        - Returns a value in ``[0.1, 0.95]`` for decomposed answers, or
          ``[0.4, 0.97]`` for direct answers, rounded to 3 decimals.
    """
    if not was_decomposed:
        # Blend: 40% structural base + 60% self-assessed (dampened).
        return round(0.4 + 0.6 * min(self_assessed, 0.95), 3)

    if not child_results:
        return 0.3

    child_confs = [r.confidence for r in child_results]
    avg_child = sum(child_confs) / len(child_confs)
    # More children = more perspectives = higher base confidence.
    coverage_bonus = min(len(child_results) * 0.05, 0.2)
    # Penalty for contradictions mentioned in deliberation.
    contradiction_penalty = 0.0
    if deliberation_notes:
        contradiction_count = deliberation_notes.lower().count("contradict")
        contradiction_penalty = min(contradiction_count * 0.05, 0.15)
    # Penalty for any cached/reused results (lower novelty).
    reused = sum(1 for r in child_results if r.reused_from_cache)
    reuse_penalty = min(reused * 0.02, 0.1)

    raw = avg_child + coverage_bonus - contradiction_penalty - reuse_penalty
    return round(max(0.1, min(raw, 0.95)), 3)


# ---------------------------------------------------------------------------
# Findings + answer formatting
# ---------------------------------------------------------------------------


def build_finding_entry(spec: AgentSpec, answer: str, confidence: float) -> KnowledgeEntry:
    """Build the knowledge-base finding for an agent's answer (pure).

    Postconditions:
        - ``finding`` is the answer truncated to ``_MAX_FINDING_CHARS`` chars;
          ``tags`` are the spec-name word stems longer than two characters.
    """
    finding = answer[:_MAX_FINDING_CHARS] if len(answer) > _MAX_FINDING_CHARS else answer
    tags = [w.lower().strip("?.,!;:") for w in spec.name.split("_") if len(w) > 2]
    return KnowledgeEntry(
        agent_id=spec.agent_id,
        agent_name=spec.name,
        focus_question=spec.focus_question,
        finding=finding,
        confidence=confidence,
        tags=tags,
    )


def collect_specialists(result: AgentResult) -> list[tuple[str, str]]:
    """Recursively collect ``(name, focus_question)`` for all child agents."""
    specialists: list[tuple[str, str]] = []
    for child in result.child_results:
        specialists.append((child.agent_name, child.focus_question))
        specialists.extend(collect_specialists(child))
    return specialists


def format_answer(result: AgentResult) -> str:
    """Append a 'Specialists consulted' footer when decomposition occurred.

    Postconditions:
        - Returns ``result.answer`` unchanged when the agent did not decompose or
          consulted no specialists; otherwise appends a markdown footer listing
          every descendant specialist.
    """
    if not result.was_decomposed:
        return result.answer

    specialists = collect_specialists(result)
    if not specialists:
        return result.answer

    footer_lines = [f"- **{name}**: {focus}" for name, focus in specialists]
    footer = "\n\n---\n**Specialists consulted:**\n" + "\n".join(footer_lines)
    return result.answer + footer
