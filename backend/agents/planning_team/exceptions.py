"""Planning-level exceptions with no dependency on Temporal or any other runtime.

Kept separate from ``planning_team.temporal.answer_signal`` so core Planning code
(``planning_team.orchestrator``) never needs to import from the ``temporal``
subpackage to catch a control-flow signal its own ``answer_callback`` contract can
raise — Planning does not need to know it is running under Temporal (see
``system_design/planning_hitl_temporal_contract.md``).
"""

from __future__ import annotations

from typing import Any, Dict, List


class PlanningAnswerPauseSignal(Exception):
    """Internal control-flow signal: no answer is available yet for a Planning
    clarification question batch.

    Raised by a callback built via
    ``planning_team.temporal.answer_signal.build_temporal_planning_answer_callback``
    when constructed with ``submitted_answers=None``. Carries the exact
    discriminated-result payload a Temporal activity wrapper needs to return to its
    calling workflow instead of blocking (mirroring
    ``software_engineering_team.pause_cycle._ActivityPauseSignal``).

    Invariants:
        - Never crosses a workflow boundary — only ever raised inside plain
          Python / activity code, caught there and translated into a
          discriminated return value (e.g. ``{"outcome": "paused", ...}``),
          never propagated into ``@workflow.defn`` code.
    """

    def __init__(self, resume_token: str, pending_questions: List[Dict[str, Any]]) -> None:
        assert isinstance(resume_token, str) and resume_token, (
            "PlanningAnswerPauseSignal requires a non-empty resume_token"
        )
        self.resume_token = resume_token
        self.pending_questions = pending_questions
        super().__init__(f"paused: resume_token={resume_token}")
