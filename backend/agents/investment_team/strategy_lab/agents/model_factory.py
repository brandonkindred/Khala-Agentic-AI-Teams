"""Resolve a Strands Model instance from environment configuration.

Priority:
  1. Ollama Cloud  (OLLAMA_API_KEY set)
  2. Ollama local  (LLM_BASE_URL points to a local server)
  3. Bedrock       (LLM_PROVIDER=bedrock)
  4. Error         (nothing configured)

Uses the existing ``llm_service.config`` resolvers so that all LLM_* env vars
are respected consistently with the rest of the platform.
"""

from __future__ import annotations

import inspect
import logging
import math
import os
from typing import Any, Dict, Optional

from llm_service.config import resolve_base_url, resolve_model, resolve_provider, resolve_timeout

logger = logging.getLogger(__name__)

# Last-resort transport timeout (seconds) when even ``resolve_timeout`` returns a
# non-positive / non-finite value. Mirrors the platform default so the resolver's
# "positive, finite float" postcondition holds unconditionally.
_DEFAULT_TRANSPORT_TIMEOUT = 900.0


def structured_output_enabled() -> bool:
    """Resolve the master toggle for schema-constrained LLM decoding.

    Reads ``STRATEGY_LAB_STRUCTURED_OUTPUT_ENABLED`` (default ``true``;
    accepted truthy values are ``"true"`` / ``"1"`` / ``"yes"``, case-
    insensitive; anything else disables). When disabled, ``get_strands_model``
    ignores any ``response_schema`` and the agents fall back to their prior
    prompt-only JSON behaviour (still validated by pydantic downstream).

    Preconditions: none.
    Postconditions: returns a ``bool``; never raises.
    """
    raw = os.environ.get("STRATEGY_LAB_STRUCTURED_OUTPUT_ENABLED", "true")
    return raw.strip().lower() in {"true", "1", "yes"}


def _resolve_strands_timeout(agent_key: str) -> float:
    """Resolve the transport-level timeout (seconds) for the strands client.

    ``STRATEGY_LAB_LLM_TIMEOUT`` takes precedence; otherwise falls back to the
    platform-wide ``resolve_timeout`` (which honours ``LLM_TIMEOUT``).

    Preconditions: ``agent_key`` is a non-empty model key.
    Postconditions: returns a **positive, finite** float, unconditionally.
    Garbage, non-positive, or non-finite (``inf`` parses cleanly and ``inf > 0``
    is ``True``, so an infinite read timeout would never cancel a hung call) env
    values fall back to ``resolve_timeout``; a non-positive/non-finite
    ``resolve_timeout`` result in turn falls back to
    ``_DEFAULT_TRANSPORT_TIMEOUT`` so the contract holds even if the platform
    resolver misbehaves.
    """
    raw = os.environ.get("STRATEGY_LAB_LLM_TIMEOUT")
    if raw is not None and raw.strip() != "":
        try:
            parsed = float(raw)
        except ValueError:
            pass
        else:
            if parsed > 0 and math.isfinite(parsed):
                return parsed
    resolved = resolve_timeout(agent_key)
    if resolved > 0 and math.isfinite(resolved):
        return resolved
    logger.warning(
        "resolve_timeout(%s) returned a non-positive/non-finite value %r; "
        "falling back to the default transport timeout %.0fs.",
        agent_key,
        resolved,
        _DEFAULT_TRANSPORT_TIMEOUT,
    )
    return _DEFAULT_TRANSPORT_TIMEOUT


def _accepts_kwarg(target: Any, name: str) -> bool:
    """Return ``True`` iff ``target``'s signature declares an *explicit* keyword
    parameter ``name``.

    A name reachable only through ``**kwargs`` (``VAR_KEYWORD``) does **not**
    count. The strands model constructors accept arbitrary ``**model_config``
    and merely *warn* on unknown keys (``validate_config_keys``) before silently
    dropping them — so a kwarg routed there never reaches the transport.
    Probing the constructor for a ``TypeError`` (the previous strategy) is
    therefore useless: an unknown kwarg is swallowed, not rejected. Introspecting
    for an explicitly-declared parameter is the only reliable signal that the
    installed SDK will actually honour the argument.

    Preconditions: ``target`` is introspectable (a class or callable); ``name``
    is a non-empty string.
    Postconditions: returns a ``bool``; never raises — an un-introspectable
    target degrades to ``False``.
    """
    try:
        params = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return False
    param = params.get(name)
    return param is not None and param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def _construct_ollama_with_timeout(model_cls, timeout: float, **kwargs):
    """Construct a strands ``OllamaModel``, forwarding ``timeout`` as the
    transport (httpx) read/connect timeout.

    strands' ``OllamaModel`` passes ``ollama_client_args`` straight to
    ``ollama.AsyncClient(host, **client_args)``, which forwards them to httpx —
    where ``timeout`` is the read/connect timeout, the only mechanism that
    actually cancels a hung HTTP call. A bare ``timeout=`` (or ``client_args=``)
    kwarg is swallowed by the constructor's ``**model_config``, which only
    *warns* and then drops it, so it must never be used. The current strands
    range (>=1.35,<2.0) declares ``ollama_client_args``; ``client_args`` (the
    client-args name strands' *other* model providers use) is probed only as a
    defensive fallback should a release within the pinned range unify on it. The
    timeout is forwarded through whichever the installed signature explicitly
    declares, degrading to "no transport timeout" (envelope wall-clock guard
    only) if neither exists.

    Preconditions: ``model_cls`` is the strands ``OllamaModel`` class (or a
    stand-in); ``timeout > 0``.
    Postconditions: returns a constructed model. The returned model carries the
    transport timeout iff ``model_cls`` exposes a client-args channel.
    """
    for param in ("ollama_client_args", "client_args"):
        if _accepts_kwarg(model_cls, param):
            return model_cls(**kwargs, **{param: {"timeout": timeout}})
    logger.warning(
        "Strands OllamaModel exposes no client-args channel for a transport "
        "timeout; relying on the envelope wall-clock guard only."
    )
    return model_cls(**kwargs)


def _construct_bedrock_with_timeout(model_cls, timeout: float, **kwargs):
    """Construct a strands ``BedrockModel``, forwarding ``timeout`` as the
    botocore read/connect timeout via ``boto_client_config``.

    A bare ``timeout=`` kwarg is swallowed by the constructor's
    ``**model_config`` (warned then dropped), so the timeout must travel through
    the explicit ``boto_client_config`` parameter as a botocore ``Config``.
    Degrades to "no transport timeout" if the SDK exposes no such parameter.

    Preconditions: ``model_cls`` is the strands ``BedrockModel`` class (or a
    stand-in); ``timeout > 0``.
    Postconditions: returns a constructed model. The returned model carries the
    transport timeout iff ``model_cls`` exposes a ``boto_client_config``
    parameter.
    """
    if _accepts_kwarg(model_cls, "boto_client_config"):
        # botocore is a hard dependency of the strands Bedrock path, so this
        # import only runs when a real Bedrock model is being constructed.
        from botocore.config import Config as BotocoreConfig

        # botocore accepts float read/connect timeouts; forward ``timeout``
        # verbatim. (Do NOT ``int()`` it — that truncates a sub-second timeout
        # to ``0`` and diverges from the Ollama path, which preserves the float.)
        client_config = BotocoreConfig(read_timeout=timeout, connect_timeout=timeout)
        return model_cls(**kwargs, boto_client_config=client_config)
    logger.warning(
        "Strands BedrockModel exposes no boto_client_config channel for a "
        "transport timeout; relying on the envelope wall-clock guard only."
    )
    return model_cls(**kwargs)


def get_strands_model(
    agent_key: str = "strategy_ideation",
    *,
    timeout: Optional[float] = None,
    response_schema: Optional[Dict[str, Any]] = None,
):
    """Return a Strands ``Model`` instance for the given agent key.

    The Strands SDK defaults to BedrockModel when ``model`` is a string.
    This factory explicitly constructs the correct provider so that Bedrock
    is only used when ``LLM_PROVIDER=bedrock`` is set.

    ``timeout`` (seconds) is the transport-level read timeout forwarded to the
    underlying client — the only mechanism that actually cancels a hung HTTP
    call. Defaults to ``STRATEGY_LAB_LLM_TIMEOUT`` / ``resolve_timeout``. The
    Strategy Lab LLM envelope adds a secondary wall-clock guard on top.

    ``response_schema`` is an optional JSON Schema (``dict``) for
    schema-constrained decoding. When supplied *and* structured output is
    enabled (:func:`structured_output_enabled`) *and* the provider is Ollama,
    it is passed to the model via the ``format`` request field so the decoder
    can only emit conforming JSON. It is a no-op for Bedrock (which keeps
    prompt-only JSON) and when the toggle is off, so callers can pass a schema
    unconditionally.

    Preconditions: ``agent_key`` is a non-empty model key; ``response_schema``,
    if given, is a JSON-serializable schema dict. ``timeout``, if passed
    explicitly, must be a positive, finite number of seconds — a non-positive or
    non-finite value raises ``ValueError`` (a resolved timeout is guaranteed
    positive and finite by :func:`_resolve_strands_timeout`).
    Postconditions: returns a constructed strands model. Adding a schema never
    changes which provider/model is selected — only whether the Ollama request
    carries a ``format`` constraint.
    """
    provider = resolve_provider()
    model_id = resolve_model(agent_key)
    base_url = resolve_base_url()
    if timeout is None:
        timeout = _resolve_strands_timeout(agent_key)
    # Boundary enforcement of the construction helpers' ``timeout > 0``
    # precondition: a non-positive or non-finite transport timeout is a caller
    # bug (an explicit bad kwarg), never a value we should forward to
    # httpx/botocore. Use an explicit raise rather than ``assert`` so the guard
    # survives ``python -O``.
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be a positive, finite number of seconds (got {timeout!r})")

    use_schema = response_schema is not None and structured_output_enabled()

    if provider == "bedrock":
        from strands.models import BedrockModel

        logger.info("Strands model: Bedrock model_id=%s timeout=%.0fs", model_id, timeout)
        return _construct_bedrock_with_timeout(BedrockModel, timeout, model_id=model_id)

    if provider == "dummy":
        raise ValueError(
            "LLM_PROVIDER=dummy is not supported for Strands agents. "
            "Set LLM_PROVIDER=ollama or LLM_PROVIDER=bedrock."
        )

    # Provider is "ollama" (the default).
    # The ``ollama`` Python package auto-reads OLLAMA_API_KEY for Bearer auth
    # and OLLAMA_HOST for the host URL, but we also honour LLM_BASE_URL and
    # LLM_OLLAMA_API_KEY from the existing llm_service config.
    from strands.models import OllamaModel

    host = os.environ.get("OLLAMA_HOST") or base_url
    api_key = (
        os.environ.get("OLLAMA_API_KEY") or os.environ.get("LLM_OLLAMA_API_KEY") or ""
    ).strip()

    if not api_key and "ollama.com" in host:
        raise ValueError(
            "Ollama Cloud requires an API key. Set OLLAMA_API_KEY (or "
            "LLM_OLLAMA_API_KEY), or point LLM_BASE_URL / OLLAMA_HOST "
            "to a local Ollama server (e.g. http://localhost:11434)."
        )

    logger.info(
        "Strands model: Ollama model_id=%s host=%s cloud=%s timeout=%.0fs schema=%s",
        model_id,
        host,
        bool(api_key),
        timeout,
        use_schema,
    )
    extra: Dict[str, Any] = {}
    if use_schema:
        # strands' OllamaModel merges ``additional_args`` straight into the
        # ``ollama`` client request, and the client accepts a JSON-Schema dict
        # for ``format`` (the same field strands' own ``structured_output``
        # uses). Unknown config keys only warn — they never raise — so this is
        # safe across the supported strands range.
        extra["additional_args"] = {"format": response_schema}
    return _construct_ollama_with_timeout(
        OllamaModel, timeout, host=host, model_id=model_id, **extra
    )
