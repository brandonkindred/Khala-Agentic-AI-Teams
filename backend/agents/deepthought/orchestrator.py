"""DeepthoughtOrchestrator — manages the recursive agent tree."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any

from deepthought.agent import DEFAULT_AGENT_BUDGET, DeepthoughtAgent
from deepthought.knowledge_base import SharedKnowledgeBase
from deepthought.models import (
    AgentEvent,
    AgentEventType,
    AgentResult,
    AgentSpec,
    DecompositionStrategy,
    DeepthoughtRequest,
    DeepthoughtResponse,
)
from deepthought.prompts import CLASSIFY_QUESTION_SYSTEM_PROMPT
from deepthought.result_cache import ResultCache

logger = logging.getLogger(__name__)

# Module-level result cache shared across requests (survives request lifecycle).
_global_result_cache = ResultCache()


class DeepthoughtOrchestrator:
    """Top-level controller that creates the root agent and tracks metrics."""

    def __init__(
        self,
        *,
        llm: Any = None,
        agent_budget: int = DEFAULT_AGENT_BUDGET,
        result_cache: ResultCache | None = None,
    ) -> None:
        if llm is not None:
            self._llm = llm
        else:
            from strands import Agent

            from llm_service import get_strands_model

            self._llm = Agent(
                model=get_strands_model("deepthought"),
                system_prompt=CLASSIFY_QUESTION_SYSTEM_PROMPT,
            )

        self._agent_budget = agent_budget
        self._result_cache = result_cache if result_cache is not None else _global_result_cache
        self._agents_spawned = 0
        self._max_depth_reached = 0
        self._events: list[AgentEvent] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def process_message(self, request: DeepthoughtRequest) -> DeepthoughtResponse:
        """Run the full recursive analysis for a user message."""
        self._agents_spawned = 0
        self._max_depth_reached = 0
        self._events = []

        # Classify question to determine decomposition strategy
        strategy = self._resolve_strategy(request)

        # Fresh knowledge base per request
        knowledge_base = SharedKnowledgeBase()

        root_spec = AgentSpec(
            agent_id=str(uuid.uuid4()),
            name="general_analyst",
            role_description=(
                "General analyst who assesses complex questions and identifies "
                "what specialist knowledge is needed to provide a comprehensive answer"
            ),
            focus_question=request.message,
            depth=0,
            parent_id=None,
        )

        # Count root as first agent
        self._register_spawn(root_spec)

        root_agent = DeepthoughtAgent(
            spec=root_spec,
            llm=self._llm,
            parent_question="",
            original_query=request.message,
            conversation_history=request.conversation_history,
            decomposition_strategy=strategy,
            knowledge_base=knowledge_base,
            result_cache=self._result_cache,
            on_agent_spawned=self._register_spawn,
            on_event=self._collect_event,
        )

        result = root_agent.execute(max_depth=request.max_depth)

        answer = self._format_answer(result)

        return DeepthoughtResponse(
            answer=answer,
            agent_tree=result,
            total_agents_spawned=self._agents_spawned,
            max_depth_reached=self._max_depth_reached,
            knowledge_entries=knowledge_base.all_entries(),
            events=list(self._events),
        )

    # ------------------------------------------------------------------
    # Strategy classification
    # ------------------------------------------------------------------

    def _resolve_strategy(self, request: DeepthoughtRequest) -> DecompositionStrategy:
        """Determine decomposition strategy — use explicit if provided, else auto-classify."""
        if request.decomposition_strategy != DecompositionStrategy.AUTO:
            return request.decomposition_strategy

        try:
            raw = self._llm.complete(
                f"Classify this question:\n\n{request.message}",
                temperature=0.1,
                system_prompt=CLASSIFY_QUESTION_SYSTEM_PROMPT,
                think=False,
            )
            # Parse JSON from response
            cleaned = raw.strip().strip("`").strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            data = json.loads(cleaned)
            strategy_str = data.get("strategy", "auto")
            try:
                return DecompositionStrategy(strategy_str)
            except ValueError:
                return DecompositionStrategy.AUTO
        except Exception:
            logger.debug("Strategy classification failed, using AUTO", exc_info=True)
            return DecompositionStrategy.AUTO

    # ------------------------------------------------------------------
    # Budget tracking (thread-safe)
    # ------------------------------------------------------------------

    def _register_spawn(self, spec: AgentSpec) -> bool:
        """Track a newly spawned agent.  Returns False to veto if budget exhausted."""
        budget_warning_event: AgentEvent | None = None
        with self._lock:
            if self._agents_spawned >= self._agent_budget:
                logger.warning(
                    "Agent budget (%d) exhausted — vetoing agent %s at depth %d",
                    self._agent_budget,
                    spec.name,
                    spec.depth,
                )
                budget_warning_event = AgentEvent(
                    event_type=AgentEventType.BUDGET_WARNING,
                    agent_id=spec.agent_id,
                    agent_name=spec.name,
                    depth=spec.depth,
                    detail=f"Budget exhausted ({self._agent_budget}), agent vetoed",
                )
            else:
                self._agents_spawned += 1
                if spec.depth > self._max_depth_reached:
                    self._max_depth_reached = spec.depth
                logger.info(
                    "Agent spawned: %s (depth=%d, total=%d/%d)",
                    spec.name,
                    spec.depth,
                    self._agents_spawned,
                    self._agent_budget,
                )
        # Route the budget-warning through _collect_event (outside the lock) so
        # the SSE stream's monkey-patched collector sees it. The previous direct
        # append to self._events bypassed streaming and only showed up in the
        # final response payload.
        if budget_warning_event is not None:
            self._collect_event(budget_warning_event)
            return False
        return True

    def _collect_event(self, event: AgentEvent) -> None:
        """Thread-safe event collection for streaming."""
        with self._lock:
            self._events.append(event)

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_answer(result: AgentResult) -> str:
        """Append a 'Specialists consulted' footer to the answer when decomposition occurred."""
        if not result.was_decomposed:
            return result.answer

        specialists = _collect_specialists(result)
        if not specialists:
            return result.answer

        footer_lines = [f"- **{name}**: {focus}" for name, focus in specialists]
        footer = "\n\n---\n**Specialists consulted:**\n" + "\n".join(footer_lines)
        return result.answer + footer


def _collect_specialists(result: AgentResult) -> list[tuple[str, str]]:
    """Recursively collect (name, focus_question) for all child agents."""
    specialists: list[tuple[str, str]] = []
    for child in result.child_results:
        specialists.append((child.agent_name, child.focus_question))
        specialists.extend(_collect_specialists(child))
    return specialists
