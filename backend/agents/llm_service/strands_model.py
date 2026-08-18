"""Shared helper for resolving the Strands model an SE-team agent should use.

Both the v2 ``phases/*.py`` modules and the ``tool_agents/*/agent.py`` modules
share the same pattern: take an injectable ``llm`` (either a pre-built Strands
``Model``, a raw ``LLMClient`` from the orchestrator, or ``None`` for default
construction) and produce a Strands ``Model`` ready to pass to ``Agent(model=...)``.

Before this helper, that logic was duplicated across 22 sites with subtle
inconsistencies — most notably, the tool agents only checked for
``_StrandsModel`` and silently discarded raw ``LLMClient`` injections by
falling through to the default ``get_strands_model()`` path, while the phase
``_resolve_model`` helpers correctly handled the ``LLMClient`` branch via
``get_strands_model(client=llm, ...)``. Tests didn't catch the asymmetry
because ``DummyLLMClient`` doubles as a Strands ``Model``.

This module collapses the pattern to one definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from llm_service import get_strands_model


def resolve_strands_model(
    llm: Any,
    *,
    response_format: str = "json",
    agent_key: str | None = None,
    get_strands_model_fn: Callable[..., Any] | None = None,
) -> Any:
    """Resolve an injectable LLM handle to a Strands ``Model``.

    Resolution rules:

    1. If ``llm`` is already a Strands ``Model``, return it as-is. This lets
       callers pin a specific mode by passing a pre-built model.
    2. If ``llm`` is an ``LLMClient`` (e.g. ``OllamaLLMClient`` from the
       orchestrator), wrap it in an ``LLMClientModel`` with the requested
       ``response_format``. Callers' retries / telemetry / rate-limit guard
       continue to flow through the injected client.
    3. Otherwise (``None`` or anything unrecognized), construct a default
       Strands model via ``get_strands_model(agent_key, response_format=...)``.

    Parameters
    ----------
    llm:
        Either a Strands ``Model``, an ``LLMClient``, or ``None``.
    response_format:
        ``"json"`` (default) or ``"text"``. Selects the JSON / text branch of
        ``LLMClient.chat`` on the wire. See ``llm_service/strands_adapter.py``
        for details.
    agent_key:
        Forwarded to ``get_strands_model`` (branches 2 and 3) so per-agent
        model overrides (``LLM_MODEL_<agent_key>``) and cost/telemetry
        tagging keep working for callers that previously called
        ``get_strands_model("<agent_key>")`` directly. ``None`` (default)
        preserves the untagged behavior existing callers already rely on.
    get_strands_model_fn:
        Override for ``get_strands_model`` (branches 2 and 3). Many persona
        ``agent.py`` modules import ``get_strands_model`` themselves and tests
        do ``monkeypatch.setattr(<agent_module>, "get_strands_model", fake)`` —
        passing that module's own (possibly monkeypatched) name through here
        preserves that seam. ``None`` (default) uses this module's own
        ``get_strands_model``.
    """
    # Local imports keep the optional ``strands`` and ``LLMClient`` imports off
    # the module-level path — this helper is imported at import time by the
    # tool agents and the v2 phases, and we don't want to force every consumer
    # to install the Strands SDK just to import the module.
    from strands.models.model import Model as _StrandsModel  # noqa: PLC0415

    from llm_service import LLMClient as _LLMClient  # noqa: PLC0415
    from llm_service.clients.dummy import (  # noqa: PLC0415
        DummyLLMClient as _DummyLLMClient,
    )
    from llm_service.clients.dummy import (
        ensure_strands_model_registration,
    )

    _get_strands_model = get_strands_model_fn or get_strands_model

    # Cold-constructed DummyLLMClient instances skip Model attachment in
    # ``__init__`` (Strands was not loaded yet). This resolver already imported
    # ``Model``, so attach inheritance before the isinstance short-circuit so
    # Dummy continues to be returned unchanged (pre-lazy behaviour).
    if llm is not None and isinstance(llm, _DummyLLMClient):
        ensure_strands_model_registration()

    if llm is not None and isinstance(llm, _StrandsModel):
        return llm
    if llm is not None and isinstance(llm, _LLMClient):
        return _get_strands_model(agent_key, client=llm, response_format=response_format)
    return _get_strands_model(agent_key, response_format=response_format)


def model_fingerprint(model: Any) -> str:
    """Best-effort stable identifier for an already-resolved Strands model.

    The canonical attribute-probing tail for turning a resolved Strands
    ``Model`` into a short, stable identity string. ``qa_agent.agent.run``
    uses this directly for its review-result cache key;
    ``code_review_agent.transcript.model_label`` (cosmetic display) and
    ``code_review_agent.mapping._review_model_fingerprint`` (cache-key
    fingerprint, which additionally resolves a raw ``LLMClient`` via
    ``resolve_code_review_model`` before probing) both delegate to this
    helper for the same tail rather than carrying their own copy. A caller
    holding an unresolved ``llm``/``LLMClient`` handle should resolve it
    first (e.g. via :func:`resolve_strands_model`) and pass the resolved
    model here.

    Preconditions:
        - ``model`` is a resolved Strands ``Model`` (or any object exposing
          the same duck-typed attributes).
    Postconditions:
        - Returns the first non-empty ``model_id``/``model_name``/``model``
          string attribute found on ``model`` (or, for a ``dict``-shaped
          ``.config``, the same three keys within it), else
          ``type(model).__name__``. Never raises — a raising descriptor/
          property anywhere in the probe (an attribute access, ``isinstance``,
          or ``dict.get``) falls back to the type name exactly like a missing
          attribute would, rather than propagating. The value is
          identity-only — safe to hash into a cache key or log for display,
          never a secret.
    """
    try:
        for attr in ("model_id", "model_name", "model"):
            value = getattr(model, attr, None)
            if isinstance(value, str) and value:
                return value
        config = getattr(model, "config", None)
        if isinstance(config, dict):
            for key in ("model_id", "model_name", "model"):
                candidate = config.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
    except Exception:
        pass
    return type(model).__name__


def resolve_text_mode_strands_model(llm: Any) -> Any:
    """Convenience wrapper: ``resolve_strands_model(llm, response_format="text")``.

    The v2 phase modules and template-based tool agents all need text-mode
    output (their downstream consumers are template parsers like
    ``parse_planning_template`` / ``parse_review_template`` /
    ``parse_files_and_summary_template``, not ``json.loads``). Calling this
    named helper at the use site removes the need for an 8-copy
    ``_resolve_model`` trampoline per file *and* makes the text-mode intent
    legible to a grep.
    """
    return resolve_strands_model(llm, response_format="text")


def run_strands_agent(agent_factory: Callable[..., Any], model: Any, prompt: str) -> str:
    """Run a one-shot Strands agent on ``prompt`` and return its stripped text.

    The single definition of the build-agent → stringify → strip incantation,
    shared by :meth:`LlmRunner.run` (the code-v2 phases) and
    ``ToolAgentBase._run_agent`` (the tool agents), so output coercion lives in
    one place instead of being copied at both call sites.

    Preconditions:
        ``agent_factory(model=model)`` returns a callable accepting ``prompt``.
    Postconditions:
        Returns ``str(agent(prompt)).strip()``; any exception raised while
        building or running the agent propagates to the caller.
    """
    return str(agent_factory(model=model)(prompt)).strip()


@dataclass(frozen=True)
class LlmRunner:
    """Bundle the two LLM-call collaborators the shared code-v2 phase impls need.

    The shared phase implementations run a one-shot Strands ``Agent`` on a prompt
    and read back its text. That is always the same three-step incantation —
    resolve the model, build the agent, stringify+strip the result — and it needs
    two injectable collaborators so the *team* module stays the monkeypatch
    boundary for tests: the ``Agent`` class (``agent_factory``) and the model
    resolver (``resolve_model``). Bundling them here collapses that repeated pair
    (and the incantation) to a single ``runner`` parameter + :meth:`run` call.

    Invariants:
        ``agent_factory`` and ``resolve_model`` are callables; the instance is
        immutable (``frozen=True``). Build it at call time from the team module's
        (possibly monkeypatched) globals — do NOT cache one at module import, or
        a later ``monkeypatch.setattr`` on those globals will not take effect.
    """

    agent_factory: Callable[..., Any]
    resolve_model: Callable[[Any], Any]

    def run(self, llm: Any, prompt: str) -> str:
        """Resolve the model, run the agent on ``prompt``, and return stripped text.

        Preconditions:
            ``prompt`` is a non-empty string; ``llm`` is a Strands ``Model``, an
            ``LLMClient``, or ``None`` (see :func:`resolve_strands_model`).
        Postconditions:
            Returns ``str(agent(prompt)).strip()``. Any exception raised by the
            agent/model propagates to the caller (callers handle it locally).
        """
        return run_strands_agent(self.agent_factory, self.resolve_model(llm), prompt)
