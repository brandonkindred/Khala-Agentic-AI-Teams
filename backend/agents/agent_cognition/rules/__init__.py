"""Rules engine for the Agent Cognition Core.

Storage + lifecycle for advisory/enforced rules and the human-in-the-loop
proposal queue (:mod:`agent_cognition.rules.store`), the pure predicate DSL
(:mod:`agent_cognition.rules.predicate`), and the deterministic enforcement layer
(:mod:`agent_cognition.rules.enforcement`) that turns enforced rules into
allow/block decisions. Importing this package has no side effects (the Postgres
schema is registered explicitly from the unified API lifespan).
"""

from __future__ import annotations

from agent_cognition.rules.enforcement import (
    build_rule_prompt_block,
    evaluate_postcondition,
    evaluate_precondition,
    evaluate_tool_call,
)
from agent_cognition.rules.predicate import (
    Predicate,
    PredicateError,
    evaluate,
    is_valid_predicate,
    parse_predicate,
    validate_predicate,
)
from agent_cognition.rules.reflection import ReflectionReport, reflect
from agent_cognition.rules.seed_packs import SEED_PACKS, SeedRule
from agent_cognition.rules.store import (
    RuleStoreError,
    approve_proposal,
    create_proposal,
    create_rule,
    get_proposal,
    get_rule,
    install_seed_pack,
    list_active_enforced_rules,
    list_proposals,
    list_rules,
    reject_proposal,
)

__all__ = [
    # store
    "RuleStoreError",
    "approve_proposal",
    "create_proposal",
    "create_rule",
    "get_proposal",
    "get_rule",
    "install_seed_pack",
    "list_active_enforced_rules",
    "list_proposals",
    "list_rules",
    "reject_proposal",
    # enforcement
    "build_rule_prompt_block",
    "evaluate_postcondition",
    "evaluate_precondition",
    "evaluate_tool_call",
    # predicate DSL
    "Predicate",
    "PredicateError",
    "evaluate",
    "is_valid_predicate",
    "parse_predicate",
    "validate_predicate",
    # reflection (rule learning)
    "ReflectionReport",
    "reflect",
    # seed packs
    "SEED_PACKS",
    "SeedRule",
]
