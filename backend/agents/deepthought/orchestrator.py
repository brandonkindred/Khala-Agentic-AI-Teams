"""DeepthoughtOrchestrator — manages the recursive agent tree."""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from deepthought.agent import DEFAULT_AGENT_BUDGET, DeepthoughtAgent
from deepthought.models import AgentResult, AgentSpec, DeepthoughtRequest, DeepthoughtResponse

logger = logging.getLogger(__name__)


class DeepthoughtOrchestrator:
    """Top-level controller that creates the root agent and tracks metrics."""

    def __init__(self, *, llm: Any = None, agent_budget: int = DEFAULT_AGENT_BUDGET) -> None:
        if llm is not None:
            self._llm = llm
        else:
            from llm_service import get_client

            self._llm = get_client("deepthought")

        self._agent_budget = agent_budget
        self._agents_spawned = 0
        self._max_depth_reached = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def process_message(self, request: DeepthoughtRequest) -> DeepthoughtResponse:
        """Run the full recursive analysis for a user message."""
        self._agents_spawned = 0
        self._max_depth_reached = 0

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
            on_agent_spawned=self._register_spawn,
        )

        result = root_agent.execute(max_depth=request.max_depth)

        answer = self._format_answer(result)

        return DeepthoughtResponse(
            answer=answer,
            agent_tree=result,
            total_agents_spawned=self._agents_spawned,
            max_depth_reached=self._max_depth_reached,
        )

    # ------------------------------------------------------------------
    # Budget tracking (thread-safe)
    # ------------------------------------------------------------------

    def _register_spawn(self, spec: AgentSpec) -> bool:
        """Track a newly spawned agent.  Returns False to veto if budget exhausted."""
        with self._lock:
            if self._agents_spawned >= self._agent_budget:
                logger.warning(
                    "Agent budget (%d) exhausted — vetoing agent %s at depth %d",
                    self._agent_budget,
                    spec.name,
                    spec.depth,
                )
                return False
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
            return True

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
