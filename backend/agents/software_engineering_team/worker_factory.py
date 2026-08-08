"""Implementation-worker construction for the coding-team swarm.

Extracted from ``coding_team/orchestrator.py`` (issue: decompose the orchestrator
god-file into named collaborators) — pure structural move, no behavior change.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from shared.dev_models import ReviewContext
from software_engineering_team.models import StackSpec
from software_engineering_team.team_routing import _v2_team_kind_for_stack

logger = logging.getLogger(__name__)


def _v2_text_mode_llm(llm: Any) -> Any:
    """Return an LLM handle suitable for v2 phases that parse text templates."""
    # Function-level imports (not module-level) keep the optional Strands SDK and the
    # LLMClient off the orchestrator's import path, matching shared.strands_model's own
    # convention; resolved once here so all branches below share a single import.
    from llm_service import LLMClient
    from llm_service.strands_model import resolve_text_mode_strands_model

    clone = getattr(llm, "clone", None)
    if callable(clone):
        try:
            config_getter = getattr(llm, "get_config", None)
            config = config_getter() if callable(config_getter) else {}
            if isinstance(config, dict) and config.get("response_format") == "text":
                return llm
            return clone(response_format="text")
        except Exception as exc:  # noqa: BLE001 - resolve explicitly when clone fails
            # clone() raised, so we cannot reconfigure this handle in place. Returning it
            # as-is (or passing it back to the resolver) would risk leaking a JSON/structured-
            # mode model to the text template parsers — resolve_strands_model passes pre-built
            # Strands Models through unchanged. Re-resolve from the wrapped LLMClient when the
            # handle exposes one (LLMClientModel stores it as ``_client``), which yields a
            # genuine text-mode wrapper; otherwise fall through to a fresh default text model.
            # Either way, never return the original non-text handle.
            logger.warning("Could not clone v2 LLM into text mode: %s", exc)
            # Prefer a public ``client`` accessor if the model exposes one; fall back to the
            # ``_client`` attribute LLMClientModel currently stores it under. This couples to a
            # private name by necessity (no public accessor today) — guarded with getattr so a
            # future rename degrades to the default text model rather than raising.
            underlying_client = getattr(llm, "client", None) or getattr(llm, "_client", None)
            return resolve_text_mode_strands_model(underlying_client)

    if llm is None or isinstance(llm, LLMClient):
        return resolve_text_mode_strands_model(llm)
    # A non-None, non-LLMClient handle without a clone() is an opaque caller-injected model
    # (e.g. a pre-built Strands model already in the right mode); pass it through unchanged.
    return llm


def _build_implementation_worker(
    agent_id: str,
    spec: StackSpec,
    llm_getter: Callable[[str], Any],
    engine_provider: Any,
    review_context: Optional[ReviewContext] = None,
) -> Any:
    """Build a v2 specialist worker for a stack.

    The concrete implementation team-lead engine comes from the injected
    ``engine_provider`` (the software-engineering team owns those engines);
    coding_team only resolves the text-mode LLM and wraps the result in a
    ``V2TeamWorker``.

    Preconditions:
        - ``engine_provider`` is a live ``CodeEngineProvider``.
        - ``review_context`` bundles the plan's system architecture and project
          specification, when available; ``None`` means "nothing to add" so a
          caller without this context yet is unaffected.

    Postconditions: returns a ``V2TeamWorker`` (frontend/backend, ``team_lead`` from
    the provider) or a ``DevOpsTeamWorker`` (devops, ``team_lead`` constructed
    directly since ``CodeEngineProvider`` only covers frontend/backend). Raises
    ``ValueError`` for an unsupported stack and ``RuntimeError`` when no provider
    was injected for a frontend/backend stack.
    """
    kind = _v2_team_kind_for_stack(spec)
    if not kind:
        raise ValueError(
            f"Unsupported coding-team stack {spec.name!r}. "
            "Only frontend_v2, backend_v2, and devops implementation teams are available."
        )
    if kind == "devops":
        # DevOpsTeamLeadAgent isn't behind the CodeEngineProvider protocol (that
        # protocol only covers frontend/backend v2 teams), so it's constructed
        # directly here rather than via engine_provider.build_implementation_team_lead.
        # Not wrapped in _v2_text_mode_llm: that forces text-mode parsing for the v2
        # teams' template parsers, but devops agents want JSON mode
        # (complete_json_with_continuation).
        from software_engineering_team.devops_team import DevOpsTeamLeadAgent
        from software_engineering_team.devops_team_worker import DevOpsTeamWorker

        return DevOpsTeamWorker(
            agent_id=agent_id,
            stack_spec=spec,
            team_lead=DevOpsTeamLeadAgent(llm_getter(kind)),
        )
    if engine_provider is None:
        raise RuntimeError(
            "No CodeEngineProvider is configured: coding_team needs an implementation-engine "
            "provider (injected by the software-engineering team, or installed by the standalone "
            "coding-team service via set_engine_provider) to build an implementation worker."
        )
    from software_engineering_team.v2_team_worker import V2TeamWorker

    team_lead = engine_provider.build_implementation_team_lead(
        kind, _v2_text_mode_llm(llm_getter(kind))
    )
    return V2TeamWorker(
        agent_id=agent_id,
        stack_spec=spec,
        team_kind=kind,
        team_lead=team_lead,
        review_context=review_context,
    )
