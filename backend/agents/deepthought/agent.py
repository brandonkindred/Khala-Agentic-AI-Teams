"""DeepthoughtAgent — a single recursive specialist node.

Each instance has a specialist role and focus question.  It can either
answer directly or spawn child agents in parallel, then deliberate and
synthesise.  Agents share a knowledge base for deduplication and a result
cache for cross-conversation reuse.
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from deepthought.knowledge_base import SharedKnowledgeBase
from deepthought.models import (
    AgentEvent,
    AgentEventType,
    AgentResult,
    AgentSpec,
    DecompositionStrategy,
    QueryAnalysis,
    SkillRequirement,
)
from deepthought.prompts import (
    ANALYSIS_FORMAT_INSTRUCTIONS,
    ANALYSIS_SYSTEM_PROMPT_REASONING,
    ANALYSIS_USER_PROMPT,
    DELIBERATION_SYSTEM_PROMPT,
    DELIBERATION_USER_PROMPT,
    SPECIALIST_SYSTEM_PROMPT,
    STRATEGY_INSTRUCTIONS,
    SYNTHESIS_SYSTEM_PROMPT,
    SYNTHESIS_USER_PROMPT,
    format_conversation_history,
    format_specialist_results,
)

# The pure, deterministic tree-shaping logic lives in ``deepthought.reasoning``
# so the Temporal workflow can reuse it verbatim. The instance methods below
# delegate to it, keeping a single source of truth across thread and Temporal
# runtimes. ``DEFAULT_AGENT_BUDGET`` is imported by the orchestrator from
# ``reasoning`` directly.
from deepthought.reasoning import (
    ANALYSIS_KB_SUMMARY_CHARS,
    DIRECT_KB_SUMMARY_CHARS,
    MAX_CHILDREN_PER_AGENT,
    build_child_specs,
    build_finding_entry,
    compute_structural_confidence,
    parse_analysis,
    results_to_dicts,
)
from deepthought.result_cache import ResultCache
from llm_service import LLMError, complete_json_via_reasoning

logger = logging.getLogger(__name__)


class DeepthoughtAgent:
    """A single node in the Deepthought recursive agent tree."""

    def __init__(
        self,
        *,
        spec: AgentSpec,
        llm: Any,
        parent_question: str = "",
        original_query: str = "",
        conversation_history: list[dict] | None = None,
        decomposition_strategy: DecompositionStrategy = DecompositionStrategy.AUTO,
        knowledge_base: SharedKnowledgeBase | None = None,
        knowledge_summary: str | None = None,
        result_cache: ResultCache | None = None,
        on_agent_spawned: Any | None = None,
        on_event: Any | None = None,
    ) -> None:
        self.spec = spec
        self.llm = llm
        self.parent_question = parent_question
        self.original_query = original_query or spec.focus_question
        self.conversation_history = conversation_history or []
        self.decomposition_strategy = decomposition_strategy
        self.knowledge_base = knowledge_base or SharedKnowledgeBase()
        # Pre-rendered knowledge summary override. When set (the Temporal path
        # passes a bounded, workflow-rendered string), the LLM prompts use it
        # directly instead of re-rendering from ``knowledge_base`` — so the
        # activity never has to receive the whole knowledge base. ``None`` (the
        # thread path) renders from the live ``knowledge_base`` as before.
        self._knowledge_summary = knowledge_summary
        self.result_cache = result_cache
        # Callback invoked each time a child agent is created; returns False to halt.
        self._on_agent_spawned = on_agent_spawned
        # Callback for streaming events: on_event(AgentEvent) -> None
        self._on_event = on_event

    def _kb_summary(self, max_chars: int) -> str:
        """Knowledge summary for a prompt — the injected override, or a fresh render.

        When a ``knowledge_summary`` was injected (the Temporal path), it is
        authoritative: the workflow already bounded it at render time, so
        ``max_chars`` applies only to the live-``knowledge_base`` render used by the
        thread path. (Injected summaries are rendered at the analysis budget, so an
        analysis activity that falls back to a direct answer reuses that slightly
        larger summary — more context, never less; harmless on the fallback path.)
        """
        if self._knowledge_summary is not None:
            return self._knowledge_summary
        return self.knowledge_base.summary_for_prompt(max_chars=max_chars)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def execute(self, max_depth: int) -> AgentResult:
        """Run analysis -> optional decomposition -> deliberation -> synthesis."""
        self._emit(AgentEventType.AGENT_ANALYSING, "Analysing question")

        # Check result cache first
        if self.result_cache:
            cached = self.result_cache.get(self.spec.focus_question)
            if cached is not None:
                self._emit(AgentEventType.KNOWLEDGE_REUSED, "Serving from cache")
                cached_copy = cached.model_copy(
                    update={"agent_id": self.spec.agent_id, "reused_from_cache": True}
                )
                return cached_copy

        # Check knowledge base for near-duplicate work
        similar = self.knowledge_base.find_similar(self.spec.focus_question)
        if similar and self.spec.depth > 0:
            best = max(similar, key=lambda e: e.confidence)
            self._emit(
                AgentEventType.KNOWLEDGE_REUSED,
                f"Reusing finding from {best.agent_name}",
            )
            return AgentResult(
                agent_id=self.spec.agent_id,
                agent_name=self.spec.name,
                depth=self.spec.depth,
                focus_question=self.spec.focus_question,
                answer=best.finding,
                confidence=best.confidence,
                child_results=[],
                was_decomposed=False,
                reused_from_cache=True,
            )

        analysis = self._analyse(max_depth)

        # Direct answer path
        if analysis.can_answer_directly or self.spec.depth >= max_depth:
            self._emit(AgentEventType.AGENT_ANSWERING, "Answering directly")
            answer = analysis.direct_answer or ""
            if not answer and self.spec.depth >= max_depth:
                answer = self._force_direct_answer()
            confidence = self._compute_structural_confidence(
                was_decomposed=False,
                self_assessed=analysis.confidence,
                child_results=[],
            )
            # Store in knowledge base
            self._store_finding(answer, confidence)
            result = AgentResult(
                agent_id=self.spec.agent_id,
                agent_name=self.spec.name,
                depth=self.spec.depth,
                focus_question=self.spec.focus_question,
                answer=answer,
                confidence=confidence,
                child_results=[],
                was_decomposed=False,
            )
            self._cache_result(result)
            self._emit(AgentEventType.AGENT_COMPLETE, "Direct answer complete")
            return result

        # Decomposition path — spawn children in parallel
        self._emit(
            AgentEventType.AGENT_DECOMPOSING,
            f"Spawning {len(analysis.skill_requirements)} specialists",
        )
        children_specs = self._build_child_specs(analysis.skill_requirements)
        child_results = self._run_children_parallel(children_specs, max_depth)

        # Deliberation phase — review child results for contradictions/gaps
        self._emit(AgentEventType.AGENT_DELIBERATING, "Reviewing specialist results")
        deliberation_notes = self._deliberate(child_results)

        # Synthesis
        self._emit(AgentEventType.AGENT_SYNTHESISING, "Synthesising results")
        synthesised = self._synthesise(child_results, deliberation_notes)

        confidence = self._compute_structural_confidence(
            was_decomposed=True,
            self_assessed=0.0,
            child_results=child_results,
            deliberation_notes=deliberation_notes,
        )

        self._store_finding(synthesised, confidence)

        result = AgentResult(
            agent_id=self.spec.agent_id,
            agent_name=self.spec.name,
            depth=self.spec.depth,
            focus_question=self.spec.focus_question,
            answer=synthesised,
            confidence=confidence,
            child_results=child_results,
            was_decomposed=True,
            deliberation_notes=deliberation_notes,
        )
        self._cache_result(result)
        self._emit(AgentEventType.AGENT_COMPLETE, "Synthesis complete")
        return result

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def _analyse(self, max_depth: int) -> QueryAnalysis:
        """Ask the LLM whether we can answer directly or need sub-agents.

        Two-pass structured-output flow via :func:`complete_json_via_reasoning`:
        a ``think=True`` reasoning pass (``reasoning_temperature=0.3``, matching
        the previous single-call ``temperature=0.3``) that analyses the question
        in structured prose against ``ANALYSIS_SYSTEM_PROMPT_REASONING``,
        followed by a ``think=False`` formatting pass (``temperature=0.0``)
        that transcribes that prose into the ``QueryAnalysis`` JSON shape via
        ``ANALYSIS_FORMAT_INSTRUCTIONS``. Either pass failing falls back to a
        forced direct answer at confidence 0.3 rather than raising.

        Preconditions:
            * ``self.llm`` implements the reasoning-capable client interface
              expected by :func:`complete_json_via_reasoning` (``complete``
              with ``think=True`` and ``complete_json`` with ``think=False``).
            * ``max_depth`` is the recursion ceiling already resolved by the
              caller; ``self.spec`` and ``self.original_query`` are populated.
        Postconditions:
            * On success, returns a ``QueryAnalysis`` reflecting the model's
              parsed judgement. If either LLM pass raises ``LLMError`` (the
              LLM layer's own failure hierarchy — rate limits, transport
              errors, malformed responses), returns a forced-direct-answer
              fallback ``QueryAnalysis`` at ``confidence=0.3`` with no
              sub-agent skill requirements instead of raising. Any other
              exception (a programming error, not an LLM failure) propagates
              to the caller rather than being swallowed.
        """
        strategy_key = self.decomposition_strategy.value
        strategy_instruction = STRATEGY_INSTRUCTIONS.get(
            strategy_key, STRATEGY_INSTRUCTIONS["auto"]
        )

        reasoning_system = ANALYSIS_SYSTEM_PROMPT_REASONING.format(
            role_description=self.spec.role_description,
            depth=self.spec.depth,
            max_depth=max_depth,
            original_query=self.original_query,
            strategy_instruction=strategy_instruction,
            knowledge_summary=self._kb_summary(ANALYSIS_KB_SUMMARY_CHARS),
        )
        context_text = (
            f"Parent question: {self.parent_question}"
            if self.parent_question
            else "Top-level query"
        )
        reasoning_user = ANALYSIS_USER_PROMPT.format(
            context=context_text,
            conversation_context=format_conversation_history(self.conversation_history),
            question=self.spec.focus_question,
        )

        try:
            data = complete_json_via_reasoning(
                self.llm,
                reasoning_prompt=reasoning_user,
                reasoning_system_prompt=reasoning_system,
                formatting_instructions=ANALYSIS_FORMAT_INSTRUCTIONS,
                objective="analyze specialist question",
                reasoning_temperature=0.3,
                temperature=0.0,
            )
            return self._parse_analysis(data)
        except LLMError:
            logger.exception("Analysis LLM call failed for agent %s", self.spec.name)
            return QueryAnalysis(
                summary=self.spec.focus_question,
                can_answer_directly=True,
                direct_answer=self._force_direct_answer(),
                confidence=0.3,
                skill_requirements=[],
            )

    def _parse_analysis(self, data: dict[str, Any]) -> QueryAnalysis:
        """Parse raw LLM JSON into a QueryAnalysis, with defensive defaults."""
        return parse_analysis(data, self.spec.focus_question)

    def _force_direct_answer(self) -> str:
        """Produce a direct answer when depth limit hit or analysis failed."""
        system = SPECIALIST_SYSTEM_PROMPT.format(
            role_description=self.spec.role_description,
            specialist_description=self.spec.role_description,
            parent_question=self.parent_question or self.spec.focus_question,
            original_query=self.original_query,
            knowledge_summary=self._kb_summary(DIRECT_KB_SUMMARY_CHARS),
        )
        try:
            return self.llm.complete(
                f"Answer this question directly and thoroughly:\n\n{self.spec.focus_question}",
                temperature=0.5,
                system_prompt=system,
                think=True,
                objective="answer question directly",
            )
        except Exception:
            logger.exception("Force-direct LLM call failed for agent %s", self.spec.name)
            return f"Unable to provide analysis for: {self.spec.focus_question}"

    # ------------------------------------------------------------------
    # Child spawning
    # ------------------------------------------------------------------

    def _build_child_specs(self, skills: list[SkillRequirement]) -> list[AgentSpec]:
        """Create AgentSpec objects for each required specialist."""
        return build_child_specs(skills, self.spec, uuid.uuid4)

    def _run_children_parallel(self, specs: list[AgentSpec], max_depth: int) -> list[AgentResult]:
        """Execute child agents in parallel threads."""
        results: list[AgentResult] = []
        if not specs:
            return results

        def _run_child(child_spec: AgentSpec) -> AgentResult:
            # Notify orchestrator of spawn; it may veto via budget.
            if self._on_agent_spawned and not self._on_agent_spawned(child_spec):
                return AgentResult(
                    agent_id=child_spec.agent_id,
                    agent_name=child_spec.name,
                    depth=child_spec.depth,
                    focus_question=child_spec.focus_question,
                    answer="Agent budget exceeded — analysis truncated.",
                    confidence=0.0,
                    child_results=[],
                    was_decomposed=False,
                )

            child = DeepthoughtAgent(
                spec=child_spec,
                llm=self.llm,
                parent_question=self.spec.focus_question,
                original_query=self.original_query,
                conversation_history=self.conversation_history,
                decomposition_strategy=self.decomposition_strategy,
                knowledge_base=self.knowledge_base,
                result_cache=self.result_cache,
                on_agent_spawned=self._on_agent_spawned,
                on_event=self._on_event,
            )
            return child.execute(max_depth)

        max_workers = min(len(specs), MAX_CHILDREN_PER_AGENT)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run_child, s): s for s in specs}
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    results.append(future.result())
                except Exception:
                    logger.exception("Child agent %s failed", spec.name)
                    results.append(
                        AgentResult(
                            agent_id=spec.agent_id,
                            agent_name=spec.name,
                            depth=spec.depth,
                            focus_question=spec.focus_question,
                            answer=f"Error analysing: {spec.focus_question}",
                            confidence=0.0,
                            child_results=[],
                            was_decomposed=False,
                        )
                    )

        return results

    # ------------------------------------------------------------------
    # Deliberation
    # ------------------------------------------------------------------

    def _deliberate(self, child_results: list[AgentResult]) -> str:
        """Review child results for contradictions, gaps, and quality issues."""
        if len(child_results) < 2:
            return ""

        system = DELIBERATION_SYSTEM_PROMPT.format(
            role_description=self.spec.role_description,
        )
        specialist_dicts = self._results_to_dicts(child_results)
        user = DELIBERATION_USER_PROMPT.format(
            question=self.spec.focus_question,
            original_query=self.original_query,
            specialist_results=format_specialist_results(specialist_dicts),
        )

        try:
            raw = self.llm.complete(
                user,
                temperature=0.2,
                system_prompt=system,
                think=True,
                objective="deliberate specialist results",
            )
            return raw
        except Exception:
            logger.exception("Deliberation LLM call failed for agent %s", self.spec.name)
            return ""

    # ------------------------------------------------------------------
    # Synthesis
    # ------------------------------------------------------------------

    def _synthesise(self, child_results: list[AgentResult], deliberation_notes: str) -> str:
        """Merge child results into a single coherent answer."""
        system = SYNTHESIS_SYSTEM_PROMPT.format(
            role_description=self.spec.role_description,
            original_query=self.original_query,
            deliberation_notes=deliberation_notes or "(No deliberation notes.)",
        )
        specialist_dicts = self._results_to_dicts(child_results)
        user = SYNTHESIS_USER_PROMPT.format(
            question=self.spec.focus_question,
            specialist_results=format_specialist_results(specialist_dicts),
        )

        try:
            return self.llm.complete(
                user,
                temperature=0.4,
                system_prompt=system,
                think=True,
                objective="synthesise specialist answers",
            )
        except Exception:
            logger.exception("Synthesis LLM call failed for agent %s", self.spec.name)
            parts = [f"**{r.agent_name}:** {r.answer}" for r in child_results]
            return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Structural confidence
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_structural_confidence(
        *,
        was_decomposed: bool,
        self_assessed: float,
        child_results: list[AgentResult],
        deliberation_notes: str = "",
    ) -> float:
        """Derive confidence from structural signals (delegates to reasoning.compute_structural_confidence).

        Preconditions:
            - self_assessed is in [0.0, 1.0].
            - child_results may be empty.
        Postconditions:
            - Returns a float in [0.1, 0.95] when was_decomposed is True, or
              in [0.4, 0.97] when was_decomposed is False (see
              reasoning.compute_structural_confidence for the exact
              blend/weighting of the underlying signals).
        """
        return compute_structural_confidence(
            was_decomposed=was_decomposed,
            self_assessed=self_assessed,
            child_results=child_results,
            deliberation_notes=deliberation_notes,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _store_finding(self, answer: str, confidence: float) -> None:
        """Write this agent's finding to the shared knowledge base."""
        self.knowledge_base.add(build_finding_entry(self.spec, answer, confidence))

    def _cache_result(self, result: AgentResult) -> None:
        """Store result in the cross-conversation cache."""
        if self.result_cache and not result.reused_from_cache:
            self.result_cache.put(self.spec.focus_question, result)

    def _emit(self, event_type: AgentEventType, detail: str) -> None:
        """Emit a streaming event if a listener is registered."""
        if self._on_event:
            event = AgentEvent(
                event_type=event_type,
                agent_id=self.spec.agent_id,
                agent_name=self.spec.name,
                depth=self.spec.depth,
                detail=detail,
            )
            try:
                self._on_event(event)
            except Exception:
                logger.debug("Event emission failed", exc_info=True)

    @staticmethod
    def _results_to_dicts(child_results: list[AgentResult]) -> list[dict]:
        return results_to_dicts(child_results)
