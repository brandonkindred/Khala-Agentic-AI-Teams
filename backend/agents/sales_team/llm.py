"""LLM client factory for the Sales Team pod.

Thin wrapper around ``llm_service.get_client`` that pins every sales role to
its own ``agent_key`` (``sales.<role>``), giving per-role model-override and
telemetry tagging for free.

Agents accept an optional ``llm_client`` in their constructor — production
callers leave it as ``None`` and get the default factory client, while tests
inject a canned/dummy client directly.
"""

from __future__ import annotations

from typing import Final

from llm_service import LLMClient, get_client

SALES_TEAM_TAG: Final[str] = "sales"

# All valid sales role identifiers. Each becomes ``sales.<role>`` when passed
# to ``get_client``, so env overrides like ``LLM_MODEL_sales_prospector`` and
# telemetry tags stay consistent across the pod.
SALES_ROLES: Final[frozenset[str]] = frozenset(
    {
        "prospector",
        "decision_maker_mapper",
        "dossier_builder",
        "outreach",
        "outreach_critic",
        "qualifier",
        "nurture",
        "discovery",
        "proposal",
        "proposal_critic",
        "closer",
        "coach",
        "learning_engine",
    }
)


def sales_agent_key(role: str) -> str:
    """Return the canonical ``sales.<role>`` agent key for ``role``."""
    if role not in SALES_ROLES:
        raise ValueError(f"Unknown sales role {role!r}; expected one of {sorted(SALES_ROLES)}")
    return f"{SALES_TEAM_TAG}.{role}"


def get_sales_llm_client(role: str) -> LLMClient:
    """Return the shared ``LLMClient`` for a given sales role.

    In production this resolves to the cached provider client configured by
    env vars (``LLM_PROVIDER``, ``LLM_MODEL``, per-agent overrides). When
    ``LLM_PROVIDER=dummy`` (used in tests without an injected client) the
    shared ``DummyLLMClient`` is returned.
    """
    return get_client(agent_key=sales_agent_key(role))


__all__ = ["SALES_TEAM_TAG", "SALES_ROLES", "get_sales_llm_client", "sales_agent_key"]
