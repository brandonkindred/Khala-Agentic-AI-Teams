"""Resolve a Strands Model instance from environment configuration.

Priority (via ``resolve_provider()``):
  1. Ollama  (default) — routed through the hardened ``llm_service`` client,
     whose ``get_client`` treats the Postgres-backed ordered provider list as
     the SOLE source of credentials (each entry carries its own API key; there
     is NO environment-variable fallback for Ollama Cloud auth — see
     ``llm_service.factory.get_client`` / ``llm_service.provider_store``).
  2. Bedrock  (LLM_PROVIDER=bedrock)
  3. Error    (LLM_PROVIDER=dummy, or any other unsupported value)

Uses the existing ``llm_service.config`` resolvers for the non-secret bits
(provider selection, model id, base URL) so those stay consistent with the
rest of the platform. This module must NOT re-derive Ollama Cloud credential
state from raw ``OLLAMA_API_KEY``/``LLM_OLLAMA_API_KEY`` env vars — that
would diverge from ``get_client``'s provider-list resolution and misfire
against a correctly configured deployment.
"""

from __future__ import annotations

import inspect
import logging
import math
import os
from typing import Any, Optional

from llm_service.config import resolve_base_url, resolve_model, resolve_provider, resolve_timeout
from shared.env_config import env_float

logger = logging.getLogger(__name__)

# Last-resort transport timeout (seconds) when even ``resolve_timeout`` returns a
# non-positive / non-finite value. Mirrors the platform default so the resolver's
# "positive, finite float" postcondition holds unconditionally.
_DEFAULT_TRANSPORT_TIMEOUT = 3600.0

# Per-agent-key sampling temperature defaults. Only ``strategy_design`` — whose
# prompts explicitly push for novel, diverse output — samples above zero; every
# other key (critically ``strategy_ideation``, which drives the deterministic
# refinement/alignment/zero-trade-repair/analysis agents) must stay greedy.
_DEFAULT_TEMPERATURES = {"strategy_design": 0.6}


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
    # ``resolve_timeout`` is a total function — it reads an env var (defaulting
    # to "3600") and catches the only failure mode (``float()`` ValueError),
    # returning 3600.0 — so it cannot raise. No ``try/except`` wraps it by design:
    # per the project DbC rule we never try/except around a callee's contract
    # failure to hide it; a future ``resolve_timeout`` that violated its
    # ``-> float`` contract by raising should surface, not silently degrade.
    resolved = resolve_timeout(agent_key)
    # We still guard the returned *value* defensively: a misconfigured resolver
    # returning a non-numeric (str/None) would make ``resolved > 0`` raise
    # ``TypeError`` and break this function's "positive, finite float"
    # postcondition. ``bool`` is excluded because ``True``/``False`` are not
    # meaningful timeouts (and ``bool`` is an ``int`` subclass). The
    # ``isinstance`` check also gates ``math.isfinite``, which itself raises
    # ``TypeError`` on a non-number.
    if (
        isinstance(resolved, (int, float))
        and not isinstance(resolved, bool)
        and math.isfinite(resolved)
        and resolved > 0
    ):
        return resolved
    logger.warning(
        "resolve_timeout(%s) returned an invalid value %r; falling back to the "
        "default transport timeout %.0fs.",
        agent_key,
        resolved,
        _DEFAULT_TRANSPORT_TIMEOUT,
    )
    return _DEFAULT_TRANSPORT_TIMEOUT


def _resolve_temperature(agent_key: str) -> float:
    """Resolve the sampling temperature for the strands client.

    Precedence: ``STRATEGY_LAB_LLM_TEMPERATURE_<AGENT_KEY>`` (per-key override)
    beats ``STRATEGY_LAB_LLM_TEMPERATURE`` (global override) beats
    ``_DEFAULT_TEMPERATURES.get(agent_key, 0.0)`` (per-key default; every key
    other than ``strategy_design`` defaults to ``0.0``).

    Preconditions: ``agent_key`` is a non-empty model key.
    Postconditions: returns a float clamped to ``[0.0, 2.0]``, unconditionally
    (garbage, non-finite, or out-of-range env values fall back to the next
    level in the precedence chain via :func:`shared.env_config.env_float`'s own
    clamping — this function never raises on a bad environment value).
    """
    global_default = env_float(
        "STRATEGY_LAB_LLM_TEMPERATURE",
        _DEFAULT_TEMPERATURES.get(agent_key, 0.0),
        floor=0.0,
        ceiling=2.0,
    )
    return env_float(
        f"STRATEGY_LAB_LLM_TEMPERATURE_{agent_key.upper()}",
        global_default,
        floor=0.0,
        ceiling=2.0,
    )


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
    response_format: str = "json",
    temperature: Optional[float] = None,
):
    """Return a Strands ``Model`` instance for the given agent key.

    For the default **Ollama** provider this routes through the platform's
    hardened ``llm_service`` client (via :func:`llm_service.strands_adapter.
    _get_strands_model`) rather than constructing strands' native ``OllamaModel``
    directly. The llm_service client converts an empty / thinking-only / prose-
    only model turn into a real signal — it detects it, runs a reduced-thinking
    ("proof-of-change") retry ladder that ends by disabling thinking entirely, and
    raises ``LLMSemanticExhaustionError`` when the payload truly yields no content —
    instead of returning a "successful" empty string that the strategy-lab
    parser then rejects with ``"No JSON object found in LLM response"``. The
    client also resolves the model's ``num_ctx`` from ``/api/show`` (so a large
    refinement prompt is not silently truncated), caps ``max_tokens``, and adds
    rate-limit / JSON-repair handling. Telemetry and per-agent model routing
    (``LLM_MODEL_<agent_key>``) come along for free.

    ``response_format`` selects the wire mode for the Ollama path: ``"json"``
    (default) forces a JSON object on the wire and parses it — correct for every
    agent that recovers its result with ``extract_json_object`` (design, design-
    review, refinement, zero-trade-repair, alignment, analysis); ``"text"``
    returns raw content and must be used by agents that consume free-form output
    (e.g. code synthesis returns a raw Python file). Bedrock ignores it.

    The Strands SDK defaults to BedrockModel when ``model`` is a string, so the
    factory still explicitly constructs ``BedrockModel`` when
    ``LLM_PROVIDER=bedrock`` (that path is unchanged: the llm_service Ollama
    hardening is provider-specific, and Bedrock has its own empty-response
    semantics).

    ``timeout`` (seconds), when passed explicitly, is validated as a boundary
    contract and forwarded as the transport read timeout on **both** paths: on
    Bedrock via ``boto_client_config``, and on Ollama by constructing a dedicated
    (uncached) ``OllamaLLMClient`` with that timeout for the adapter. When it is
    omitted, the transport timeout is owned by the llm_service client
    (``resolve_timeout`` / ``LLM_TIMEOUT``) on the Ollama path and by
    ``_resolve_strands_timeout`` on the Bedrock path. The Strategy Lab LLM
    envelope's wall-clock guard (``STRATEGY_LAB_LLM_TIMEOUT``) bounds every call
    on top of whichever transport timeout applies.

    ``temperature``, when omitted, is resolved per ``agent_key`` via
    :func:`_resolve_temperature` — ``0.6`` for ``strategy_design`` (the only key
    whose prompts ask for sampling diversity) and ``0.0`` for every other key,
    overridable via ``STRATEGY_LAB_LLM_TEMPERATURE[_<AGENT_KEY>]``. It is
    forwarded on the Ollama path unconditionally and on the Bedrock path only if
    the installed ``BedrockModel`` declares an explicit ``temperature``
    parameter (probed via ``_accepts_kwarg``, mirroring the ``boto_client_config``
    guard).

    The JSON-shape contract on the Ollama path is enforced by the ``json_object``
    wire mode plus pydantic validation downstream (and, for the refinement agent,
    a schema embedded verbatim in its prompt). This replaces the strands-native
    decoder-level ``format`` constraint that the native ``OllamaModel`` path used.

    Preconditions: ``agent_key`` is a non-empty model key (an empty value raises
    ``ValueError``); ``response_format`` is ``"json"`` or ``"text"``. ``timeout``,
    if passed explicitly, must be a positive, finite *number* of seconds — a
    non-numeric value raises ``TypeError`` and a non-positive or non-finite value
    raises ``ValueError``. The resolved ``LLM_PROVIDER`` must be a supported
    Strands provider (``ollama`` or ``bedrock``); any other value raises
    ``ValueError`` rather than silently falling through to Ollama.
    Postconditions: returns a constructed strands ``Model``. The chosen provider
    / model never depends on ``response_format``.
    """
    # Boundary enforcement of the documented ``agent_key`` precondition: an empty
    # key is a caller bug that would otherwise surface obscurely downstream (e.g.
    # ``resolve_model`` silently returning a default). Raise (not ``assert``) so
    # the guard survives ``python -O``.
    if not agent_key or not agent_key.strip():
        raise ValueError("agent_key must be a non-empty string")

    provider = resolve_provider()
    model_id = resolve_model(agent_key)
    base_url = resolve_base_url()

    # Boundary enforcement of the transport's ``timeout > 0`` precondition for
    # an explicitly-supplied value: a bad transport timeout is a caller bug (an
    # explicit bad kwarg), never a value we should forward. Check the type first
    # so a non-numeric kwarg raises a clear ``TypeError`` (rather than an obscure
    # one from ``math.isfinite``); use explicit raises rather than ``assert`` so
    # the guards survive ``python -O``. A resolved (None) timeout is guaranteed
    # valid by :func:`_resolve_strands_timeout`, so it is not re-validated.
    if timeout is not None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise TypeError(f"timeout must be a number of seconds (got {type(timeout).__name__})")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(
                f"timeout must be a positive, finite number of seconds (got {timeout!r})"
            )

    if response_format not in ("json", "text"):
        raise ValueError(f"response_format must be 'json' or 'text', got {response_format!r}")

    resolved_temperature = (
        temperature if temperature is not None else _resolve_temperature(agent_key)
    )

    if provider == "bedrock":
        from strands.models import BedrockModel

        resolved_timeout = timeout if timeout is not None else _resolve_strands_timeout(agent_key)
        logger.info(
            "Strands model: Bedrock model_id=%s timeout=%.0fs temperature=%.2f",
            model_id,
            resolved_timeout,
            resolved_temperature,
        )
        bedrock_kwargs = {"model_id": model_id}
        if _accepts_kwarg(BedrockModel, "temperature"):
            bedrock_kwargs["temperature"] = resolved_temperature
        return _construct_bedrock_with_timeout(BedrockModel, resolved_timeout, **bedrock_kwargs)

    if provider == "dummy":
        raise ValueError(
            "LLM_PROVIDER=dummy is not supported for Strands agents. "
            "Set LLM_PROVIDER=ollama or LLM_PROVIDER=bedrock."
        )

    # Fail fast on a misconfigured provider rather than silently treating any
    # unknown value (e.g. ``LLM_PROVIDER=openai``) as Ollama. Only the supported
    # Strands providers reach the routing below.
    if provider != "ollama":
        raise ValueError(
            f"Unsupported LLM_PROVIDER: {provider!r}. Supported values: ollama, bedrock."
        )

    # Provider is "ollama" (the default). Credential validation is NOT done
    # here: the Postgres-backed ordered provider list is the sole source of
    # Ollama Cloud auth (each entry carries its own API key with no
    # environment fallback — see ``llm_service.factory.get_client`` /
    # ``llm_service.provider_store``), so re-checking ``OLLAMA_API_KEY`` /
    # ``LLM_OLLAMA_API_KEY`` against ``resolve_base_url()`` here would consult
    # a completely different (and, for a provider-list deployment, always
    # empty) source and reject a correctly configured deployment. An entry
    # that genuinely lacks a required key still fails clearly and fast — as a
    # non-retryable ``LLMPermanentError``/``LLMNotConfiguredError`` raised by
    # ``get_client`` or the first authenticated request — without this module
    # duplicating that resolution.

    # Route through the hardened llm_service path. Imported lazily so the module
    # carries no import-time dependency on strands beyond the provider branches
    # above, and so tests can monkeypatch the source attribute.
    from llm_service.strands_adapter import _get_strands_model as _llm_service_strands_model

    logger.info(
        "Strategy Lab LLM routed through llm_service: agent_key=%s model=%s host=%s "
        "response_format=%s explicit_timeout=%s temperature=%.2f",
        agent_key,
        model_id,
        base_url,
        response_format,
        timeout if timeout is not None else "-",
        resolved_temperature,
    )

    # When the caller pins an explicit transport timeout, honour it: build a
    # dedicated (uncached) client with that read timeout and hand it to the
    # adapter. Otherwise the adapter's default client owns the timeout
    # (``resolve_timeout`` / ``LLM_TIMEOUT``). Building the client only on the
    # explicit-timeout path keeps the common path on the shared client cache.
    if timeout is not None:
        from llm_service.clients import OllamaLLMClient

        explicit_client = OllamaLLMClient(model=model_id, base_url=base_url, timeout=float(timeout))
        return _llm_service_strands_model(
            agent_key=agent_key,
            response_format=response_format,
            client=explicit_client,
            temperature=resolved_temperature,
        )

    return _llm_service_strands_model(
        agent_key=agent_key, response_format=response_format, temperature=resolved_temperature
    )
