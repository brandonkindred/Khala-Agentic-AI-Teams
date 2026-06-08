"""Per-agent cognition scope resolved from the agent registry manifest.

The sync worker, scheduler, and reflection engine each need a slice of an agent's
declarative ``cognition`` config (``knowledge_graph`` ingest/grounding flags and
``memory.retention_days_events``). This module centralizes reading it from the
:mod:`agent_registry`, with safe defaults when the registry or manifest is
unavailable (e.g. an ad-hoc agent that has memory but no on-disk manifest).

Defaults are **on**: an unknown agent ingests both events and summaries, grounds
its rule proposals, and uses the platform default retention — matching the
"knowledge base attached to every new agent by default" contract. Resolution
never raises; any lookup failure degrades to the defaults.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Mirrors CognitionMemorySpec.retention_days_events / CognitionKnowledgeGraphSpec
# defaults so a manifest-less agent behaves like the default-on config.
_DEFAULT_RETENTION_DAYS = 90
_DEFAULT_INGEST_EVENTS = True
_DEFAULT_INGEST_SUMMARIES = True
_DEFAULT_GROUND_RULES = True


def _knowledge_graph_spec(agent_id: str):
    """Return the agent's ``CognitionKnowledgeGraphSpec`` or ``None``.

    ``None`` means "use defaults" — no manifest, no cognition block, or any lookup
    failure. Never raises.
    """
    try:
        from agent_registry import get_registry  # noqa: PLC0415

        manifest = get_registry().get(agent_id)
        if manifest is None or manifest.cognition is None:
            return None
        return manifest.cognition.knowledge_graph
    except Exception:
        logger.debug("manifest_scope: registry lookup failed for %s; using defaults", agent_id)
        return None


def graph_scope(agent_id: str) -> tuple[bool, bool]:
    """Return ``(ingest_events, ingest_summaries)`` for an agent.

    Postconditions:
        * ``(False, False)`` when the agent's graph is explicitly disabled
          (``knowledge_graph.enabled = False``); otherwise the per-kind ingest
          flags (defaulting to both ``True``).
    """
    spec = _knowledge_graph_spec(agent_id)
    if spec is None:
        return (_DEFAULT_INGEST_EVENTS, _DEFAULT_INGEST_SUMMARIES)
    if not spec.enabled:
        return (False, False)
    return (spec.ingest_events, spec.ingest_summaries)


def ground_rule_proposals(agent_id: str) -> bool:
    """Whether reflection should ground this agent's proposals with graph context.

    Postconditions:
        * ``False`` when the graph is disabled or grounding is turned off;
          otherwise the manifest's flag (default ``True``).
    """
    spec = _knowledge_graph_spec(agent_id)
    if spec is None:
        return _DEFAULT_GROUND_RULES
    return bool(spec.enabled and spec.ground_rule_proposals)


def retention_days(agent_id: str) -> int:
    """Raw-event retention (days) for an agent, from the manifest or the default."""
    try:
        from agent_registry import get_registry  # noqa: PLC0415

        manifest = get_registry().get(agent_id)
        if manifest is not None and manifest.cognition is not None:
            return manifest.cognition.memory.retention_days_events
    except Exception:
        logger.debug("manifest_scope: retention lookup failed for %s; using default", agent_id)
    return _DEFAULT_RETENTION_DAYS
