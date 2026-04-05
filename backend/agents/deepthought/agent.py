"""DeepthoughtAgent — a single recursive specialist node.

Each instance has a specialist role and focus question.  It can either
answer directly or spawn child agents in parallel, then synthesise.
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from deepthought.models import AgentResult, AgentSpec, QueryAnalysis, SkillRequirement
from deepthought.prompts import (
    ANALYSIS_SYSTEM_PROMPT,
    ANALYSIS_USER_PROMPT,
    SPECIALIST_SYSTEM_PROMPT,
    SYNTHESIS_SYSTEM_PROMPT,
    SYNTHESIS_USER_PROMPT,
    format_specialist_results,
)

logger = logging.getLogger(__name__)

# Maximum child agents any single node may spawn.
MAX_CHILDREN_PER_AGENT = 5

# Global budget — shared across the whole tree via the orchestrator callback.
DEFAULT_AGENT_BUDGET = 50


class DeepthoughtAgent:
    """A single node in the Deepthought recursive agent tree."""

    def __init__(
        self,
        *,
        spec: AgentSpec,
        llm: Any,
        parent_question: str = "",
        on_agent_spawned: Any | None = None,
    ) -> None:
        self.spec = spec
        self.llm = llm
        self.parent_question = parent_question
        # Callback invoked each time a child agent is created; returns False to halt.
        self._on_agent_spawned = on_agent_spawned

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def execute(self, max_depth: int) -> AgentResult:
        """Run analysis → optional decomposition → synthesis and return a result."""
        logger.info(
            "Agent %s (depth=%d) analysing: %s",
            self.spec.name,
            self.spec.depth,
            self.spec.focus_question[:120],
        )

        analysis = self._analyse(max_depth)

        # Direct answer path
        if analysis.can_answer_directly or self.spec.depth >= max_depth:
            answer = analysis.direct_answer or ""
            if not answer and self.spec.depth >= max_depth:
                answer = self._force_direct_answer()
            return AgentResult(
                agent_id=self.spec.agent_id,
                agent_name=self.spec.name,
                depth=self.spec.depth,
                focus_question=self.spec.focus_question,
                answer=answer,
                confidence=analysis.confidence,
                child_results=[],
                was_decomposed=False,
            )

        # Decomposition path — spawn children in parallel
        children_specs = self._build_child_specs(analysis.skill_requirements)
        child_results = self._run_children_parallel(children_specs, max_depth)

        synthesised = self._synthesise(child_results)

        return AgentResult(
            agent_id=self.spec.agent_id,
            agent_name=self.spec.name,
            depth=self.spec.depth,
            focus_question=self.spec.focus_question,
            answer=synthesised,
            confidence=self._aggregate_confidence(child_results),
            child_results=child_results,
            was_decomposed=True,
        )

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def _analyse(self, max_depth: int) -> QueryAnalysis:
        """Ask the LLM whether we can answer directly or need sub-agents."""
        system = ANALYSIS_SYSTEM_PROMPT.format(
            role_description=self.spec.role_description,
            depth=self.spec.depth,
            max_depth=max_depth,
        )
        context_text = (
            f"Parent question: {self.parent_question}"
            if self.parent_question
            else "Top-level query"
        )
        user = ANALYSIS_USER_PROMPT.format(
            context=context_text,
            question=self.spec.focus_question,
        )

        try:
            data = self.llm.complete_json(
                user,
                temperature=0.3,
                system_prompt=system,
                think=True,
            )
            return self._parse_analysis(data)
        except Exception:
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
        skills_raw = data.get("skill_requirements") or []
        skills = []
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
            summary=data.get("summary", self.spec.focus_question),
            can_answer_directly=can_answer,
            direct_answer=data.get("direct_answer") if can_answer else None,
            confidence=float(data.get("confidence", 0.5)) if can_answer else 0.0,
            skill_requirements=[] if can_answer else skills,
        )

    def _force_direct_answer(self) -> str:
        """Produce a direct answer when we've hit depth limits or fallback."""
        system = SPECIALIST_SYSTEM_PROMPT.format(
            role_description=self.spec.role_description,
            specialist_description=self.spec.role_description,
            parent_question=self.parent_question or self.spec.focus_question,
        )
        try:
            return self.llm.complete(
                f"Answer this question directly and thoroughly:\n\n{self.spec.focus_question}",
                temperature=0.5,
                system_prompt=system,
                think=True,
            )
        except Exception:
            logger.exception("Force-direct LLM call failed for agent %s", self.spec.name)
            return f"Unable to provide analysis for: {self.spec.focus_question}"

    # ------------------------------------------------------------------
    # Child spawning
    # ------------------------------------------------------------------

    def _build_child_specs(self, skills: list[SkillRequirement]) -> list[AgentSpec]:
        """Create AgentSpec objects for each required specialist."""
        specs = []
        for skill in skills[:MAX_CHILDREN_PER_AGENT]:
            spec = AgentSpec(
                agent_id=str(uuid.uuid4()),
                name=skill.name,
                role_description=skill.description,
                focus_question=skill.focus_question,
                depth=self.spec.depth + 1,
                parent_id=self.spec.agent_id,
            )
            specs.append(spec)
        return specs

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
                on_agent_spawned=self._on_agent_spawned,
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
    # Synthesis
    # ------------------------------------------------------------------

    def _synthesise(self, child_results: list[AgentResult]) -> str:
        """Merge child results into a single coherent answer."""
        system = SYNTHESIS_SYSTEM_PROMPT.format(
            role_description=self.spec.role_description,
        )
        specialist_dicts = [
            {
                "agent_name": r.agent_name,
                "focus_question": r.focus_question,
                "confidence": r.confidence,
                "answer": r.answer,
            }
            for r in child_results
        ]
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
            )
        except Exception:
            logger.exception("Synthesis LLM call failed for agent %s", self.spec.name)
            # Fallback: concatenate child answers.
            parts = [f"**{r.agent_name}:** {r.answer}" for r in child_results]
            return "\n\n".join(parts)

    @staticmethod
    def _aggregate_confidence(child_results: list[AgentResult]) -> float:
        """Compute a weighted average confidence from child results."""
        if not child_results:
            return 0.0
        total = sum(r.confidence for r in child_results)
        return round(total / len(child_results), 3)
