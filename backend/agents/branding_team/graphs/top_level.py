"""Top-level branding pipeline graph.

Wires Phase 1–5 sub-graphs into a single ``GraphBuilder`` Graph with
conditional edges for phase gating and optional adapter nodes.
"""

from __future__ import annotations

from typing import Optional

from strands.multiagent.graph import Graph

from branding_team.graphs.phase1_strategic_core import build_phase1_graph
from branding_team.graphs.phase2_narrative import build_phase2_graph
from branding_team.graphs.phase3_visual import build_phase3_graph
from branding_team.graphs.phase4_channel import build_phase4_graph
from branding_team.graphs.phase5_governance import build_phase5_graph
from branding_team.graphs.shared import phase_index
from branding_team.models import BrandPhase
from shared.graph import build_sequential

# BrandPhase ends with COMPLETE (not a runnable pipeline stage).
_MAX_RUNNABLE_PHASE_INDEX = len(BrandPhase) - 2
# Outer graph budget covers all five phases; per-node budget covers one phase sub-graph.
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 600.0
DEFAULT_NODE_TIMEOUT_SECONDS = 180.0


def build_branding_graph(
    *,
    target_phase: Optional[BrandPhase] = None,
) -> Graph:
    """Build the top-level branding pipeline graph.

    Phase 1 always runs and is the entry point; each later phase is appended (and
    edged onto the previous phase) only while its index is ``<= stop_idx``, where
    ``stop_idx`` comes from ``phase_index(target_phase)`` or defaults to the last
    runnable phase. Phases therefore form a single linear chain up to the target.

    Preconditions:
        ``target_phase`` is either ``None`` or a runnable ``BrandPhase`` (i.e. one
        for which ``phase_index`` returns a valid index; ``BrandPhase.COMPLETE`` is
        not a runnable stage).
    Postconditions:
        Returns a built Strands ``Graph`` whose entry point is Phase 1 and which
        contains exactly the phases up to and including ``target_phase`` (all five
        when ``target_phase`` is ``None``), each wired to its predecessor. The
        graph carries the module's execution/node timeout budgets and is ready to
        invoke with the serialised ``BrandingMission`` as its task string.
    """
    stop_idx = phase_index(target_phase) if target_phase else _MAX_RUNNABLE_PHASE_INDEX

    # ---- Phase 1: Strategic Core (always runs) ----
    stages: list[tuple[str, Graph]] = [
        ("phase1_strategic_core", build_phase1_graph()),
    ]

    # ---- Phase 2: Narrative & Messaging ----
    if stop_idx >= 1:
        stages.append(("phase2_narrative", build_phase2_graph()))

    # ---- Phase 3: Visual & Expressive Identity ----
    if stop_idx >= 2:
        stages.append(("phase3_visual", build_phase3_graph()))

    # ---- Phase 4: Experience & Channel Activation ----
    if stop_idx >= 3:
        stages.append(("phase4_channel", build_phase4_graph()))

    # ---- Phase 5: Governance & Evolution ----
    if stop_idx >= 4:
        stages.append(("phase5_governance", build_phase5_graph()))

    return build_sequential(
        stages=stages,
        graph_id="branding_pipeline",
        execution_timeout=DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        node_timeout=DEFAULT_NODE_TIMEOUT_SECONDS,
    )
