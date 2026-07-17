"""Synthesis agent: fold market-research (or other) evidence into the client context.

Deterministic (no LLM). Merges evidence summary/insights into
``ClientContext.constraints`` when both an evidence payload and a client context exist.
"""

from __future__ import annotations

from planning_team.agents.synthesis.models import SynthesisInput, SynthesisOutput
from planning_team.models import ClientContext
from planning_team.phases._util import as_client_context


class SynthesisAgent:
    """Stateless agent that consolidates evidence into the client context.

    Invariants:
        - Holds no mutable state; a single instance is safe to reuse across runs.
    """

    def run(self, input_data: SynthesisInput) -> SynthesisOutput:
        """Merge evidence into the context, mirroring the legacy phase branches.

        Preconditions:
            - ``input_data.market_research_evidence`` is ``None`` or a mapping with
              optional ``summary``/``insights`` keys.
        Postconditions:
            - When no evidence: ``evidence_attached`` is ``False`` and no context update.
            - When evidence but no client context: ``evidence_attached`` is ``True`` and
              ``client_context`` stays ``None``.
            - When evidence + client context with a summary/insights: ``client_context``
              is the updated context with ``market_research_summary``/``_insights``
              folded into ``constraints``.
        """
        context = {"client_context": input_data.client_context}
        market_research_evidence = input_data.market_research_evidence

        if not market_research_evidence:
            return SynthesisOutput(evidence=market_research_evidence, evidence_attached=False)

        client_context = as_client_context(context.get("client_context"))
        if client_context is None:
            return SynthesisOutput(evidence=market_research_evidence, evidence_attached=True)

        updated: ClientContext | None = None
        summary = market_research_evidence.get("summary")
        insights = market_research_evidence.get("insights", [])
        if summary or insights:
            constraints = dict(client_context.constraints or {})
            constraints["market_research_summary"] = summary or ""
            constraints["market_research_insights"] = insights
            dump = client_context.model_dump()
            dump["constraints"] = constraints
            updated = ClientContext(**dump)

        return SynthesisOutput(
            evidence=market_research_evidence,
            evidence_attached=True,
            client_context=updated,
        )
