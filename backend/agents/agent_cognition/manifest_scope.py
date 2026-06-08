"""Per-agent cognition scope resolved from the agent registry manifest.

The sync worker, scheduler, and reflection engine each need a slice of an agent's
declarative ``cognition`` config (``knowledge_graph`` ingest/grounding flags and
``memory.retention_days_events``). This module centralizes reading it from the
:mod:`agent_registry`, with safe defaults when the registry or manifest is
unavailable (e.g. an ad-hoc agent that has memory but no on-disk manifest).

Defaults come straight from the manifest model classes (``CognitionKnowledgeGraphSpec``
/ ``CognitionMemorySpec``), so a manifest-less agent behaves exactly like an agent
declaring the default block — and a change to a model default can never silently
diverge from the manifest-less path. Resolution never raises; any lookup failure
degrades to those defaults.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _kg_default() -> Any:
    """The default ``CognitionKnowledgeGraphSpec`` (default-on), cached."""
    from agent_registry.models import CognitionKnowledgeGraphSpec  # noqa: PLC0415

    return CognitionKnowledgeGraphSpec()


@lru_cache(maxsize=1)
def _retention_default() -> int:
    """The default raw-event retention from ``CognitionMemorySpec``, cached."""
    from agent_registry.models import CognitionMemorySpec  # noqa: PLC0415

    return CognitionMemorySpec().retention_days_events


def _knowledge_graph_spec(agent_id: str) -> Any:
    """Return the agent's ``CognitionKnowledgeGraphSpec``, or the default.

    Falls back to the model default when there is no manifest, no cognition block,
    or any lookup failure — so callers always get a spec to read. Never raises.
    """
    try:
        from agent_registry import get_registry  # noqa: PLC0415

        manifest = get_registry().get(agent_id)
        if manifest is not None and manifest.cognition is not None:
            return manifest.cognition.knowledge_graph
    except Exception:
        logger.debug("manifest_scope: registry lookup failed for %s; using defaults", agent_id)
    return _kg_default()


def graph_scope(agent_id: str) -> tuple[bool, bool]:
    """Return ``(ingest_events, ingest_summaries)`` for an agent.

    Postconditions:
        * ``(False, False)`` when the agent's graph is disabled
          (``knowledge_graph.enabled = False``); otherwise the per-kind ingest
          flags. A manifest-less agent uses the model default (both ``True``).
    """
    spec = _knowledge_graph_spec(agent_id)
    if not spec.enabled:
        return (False, False)
    return (spec.ingest_events, spec.ingest_summaries)


def ground_rule_proposals(agent_id: str) -> bool:
    """Whether reflection should ground this agent's proposals with graph context.

    Postconditions:
        * ``False`` when the graph is disabled or grounding is turned off;
          otherwise the manifest's flag (model default ``True``).
    """
    spec = _knowledge_graph_spec(agent_id)
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
    return _retention_default()
