"""Intake agent: build the initial ``ClientContext`` from request inputs.

Deterministic (no LLM, no tools). §2 coordinator logic for the intake phase.
"""

from __future__ import annotations

from planning_team.agents.intake.models import IntakeInput, IntakeOutput
from planning_team.models import ClientContext


class IntakeAgent:
    """Stateless agent that seeds a planning run's ``ClientContext``.

    Invariants:
        - Holds no mutable state; a single instance is safe to reuse across runs.
    """

    def run(self, input_data: IntakeInput) -> IntakeOutput:
        """Assemble the initial context from client name, brief, spec, and artifacts.

        Preconditions:
            - ``input_data`` is a valid ``IntakeInput`` (``repo_path`` is a string).
        Postconditions:
            - Returns an ``IntakeOutput`` whose ``client_context`` carries the raw
              brief/spec and existing artifacts, and whose ``initial_brief``/
              ``spec_content`` are normalized to ``""`` when the request omitted them.
        """
        client_context = ClientContext(
            client_name=input_data.client_name,
            raw_brief=input_data.initial_brief,
            raw_spec=input_data.spec_content,
            existing_artifacts=input_data.existing_artifacts or [],
        )
        return IntakeOutput(
            client_context=client_context,
            repo_path=input_data.repo_path,
            initial_brief=input_data.initial_brief or "",
            spec_content=input_data.spec_content or "",
        )
