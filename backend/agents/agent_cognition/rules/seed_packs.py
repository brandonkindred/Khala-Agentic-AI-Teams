"""Seed rule packs for the Agent Cognition Core (install mechanism).

A *seed pack* is a named set of rules installed onto an agent at provision time
so it has sensible day-one guardrails. This module is the **mechanism +
registry**: it declares the :data:`SEED_PACKS` catalog and the small
:class:`SeedRule` shape the installer
(:func:`agent_cognition.rules.store.install_seed_pack`) materializes into
``agent_cognition_rules`` rows. The catalog ships intentionally minimal here; the
full guardrail content is authored in a later step.

Each seed rule carries a stable ``key`` (unique within its pack) so installs are
idempotent — the store records ``{"seed_pack": <name>, "seed_key": <key>}`` in the
rule's ``evidence`` and skips a rule already present for the agent.

Importing this module has no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_cognition.models import RuleMode

__all__ = ["SeedRule", "SEED_PACKS"]


@dataclass(frozen=True)
class SeedRule:
    """One rule in a seed pack.

    Invariant: ``key`` is unique within its pack and stable across releases — it
    is the idempotency identity the installer records in ``evidence``.
    """

    key: str
    text: str
    mode: RuleMode = RuleMode.ADVISORY
    predicate: dict[str, Any] = field(default_factory=dict)
    rationale: str | None = None
    priority: int = 0


# Named catalog. Keep entries minimal and obviously safe — the comprehensive
# guardrail content lands in a later step; this exists so the install mechanism
# is exercisable today against the pack name agents reference in their manifest.
SEED_PACKS: dict[str, list[SeedRule]] = {
    "default_guardrails": [
        SeedRule(
            key="no-secrets-in-output",
            text="Never include credentials, API keys, tokens, or other secrets in your output.",
            mode=RuleMode.ADVISORY,
            rationale="Prevents leaking sensitive material into persisted memory or responses.",
            priority=100,
        ),
    ],
}
