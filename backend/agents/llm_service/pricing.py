"""Token → USD cost estimation for LLM calls.

A single, intentionally small pricing table maps model names to a per-1k-token
price for input (prompt) and output (completion) tokens. ``estimate_cost_usd``
is a pure function used by :mod:`llm_service.telemetry` to stamp ``cost.usd`` on
every call record/span, and by the Software Engineering team's per-job cost
accounting.

Prices are best-effort estimates for the models this platform runs (Ollama
Cloud / self-hosted). They are **not** authoritative billing figures and are
deliberately easy to override per model via an environment variable:

    LLM_PRICE_<NORMALIZED_MODEL>=<usd_per_1k_input>/<usd_per_1k_output>

where ``<NORMALIZED_MODEL>`` is the model name uppercased with every run of
non-alphanumeric characters collapsed to a single underscore. For example::

    deepseek-v4-pro:cloud  ->  LLM_PRICE_DEEPSEEK_V4_PRO_CLOUD=0.0003/0.0012

An unknown model (no table entry, no override) costs ``$0`` and logs at DEBUG —
a missing price surfaces as a visible zero rather than a fabricated number.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPrice:
    """Per-1k-token price for a model.

    Invariants:
        - ``usd_per_1k_input`` and ``usd_per_1k_output`` are finite and ``>= 0``.
    """

    usd_per_1k_input: float
    usd_per_1k_output: float


# Best-effort estimates (USD per 1,000 tokens). Local llama* models run on
# self-hosted hardware and are priced at $0 here (compute cost is not metered
# per token). Cloud models use rough public list prices; override per
# deployment with LLM_PRICE_<model> when real rates are known.
# Wrapped read-only (MappingProxyType) so accidental mutation can't corrupt cost
# estimates; override per model via the LLM_PRICE_* env var instead.
MODEL_PRICING: Mapping[str, ModelPrice] = MappingProxyType(
    {
        "deepseek-v4-pro:cloud": ModelPrice(0.00027, 0.00110),
        "qwen3-coder:480b-cloud": ModelPrice(0.00020, 0.00080),
        "qwen3-coder:480b": ModelPrice(0.00020, 0.00080),
        "qwen3.5:397b": ModelPrice(0.00020, 0.00080),
        "qwen3.5:397b-cloud": ModelPrice(0.00020, 0.00080),
        "qwen3.5:cloud": ModelPrice(0.00020, 0.00080),
        "llama3.1": ModelPrice(0.0, 0.0),
        "llama3.2": ModelPrice(0.0, 0.0),
    }
)

_ENV_PREFIX = "LLM_PRICE_"
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def _normalize_model_for_env(model: str) -> str:
    """Map a model name to the suffix used in its ``LLM_PRICE_*`` env var.

    Preconditions:
        - ``model`` is a string.
    Postconditions:
        - Returns ``model`` uppercased with each run of non-alphanumeric
          characters collapsed to one ``_`` and leading/trailing ``_`` stripped.

    Note: the normalization is intentionally lossy (env var names can't carry
    ``:``/``-``), so model ids differing only in separators collide — e.g.
    ``deepseek-v4-pro:cloud`` and ``deepseek_v4_pro_cloud`` both map to
    ``DEEPSEEK_V4_PRO_CLOUD``. This only affects the optional ``LLM_PRICE_*``
    override convenience (the built-in ``MODEL_PRICING`` table is keyed by the
    exact model string and never collides); operators with two such near-identical
    ids should price them via the table rather than the env override.
    """
    return _NON_ALNUM.sub("_", model.upper()).strip("_")


def _price_for_model(model: str) -> ModelPrice | None:
    """Resolve the effective price for ``model``: env override, then table.

    Postconditions:
        - Returns a :class:`ModelPrice` when an override or table entry exists,
          else ``None``. A malformed override is ignored (logged at WARNING) and
          resolution falls back to the table.
    """
    env_key = _ENV_PREFIX + _normalize_model_for_env(model)
    raw = os.environ.get(env_key)
    if raw:
        try:
            in_str, out_str = raw.split("/", 1)
            in_val, out_val = float(in_str), float(out_str)
            # Reject non-finite (inf/nan) so a typo can't produce an infinite cost
            # that poisons spans, the cost counter, and the per-job total.
            if not (math.isfinite(in_val) and math.isfinite(out_val)):
                raise ValueError("non-finite price")
            return ModelPrice(max(0.0, in_val), max(0.0, out_val))
        except (ValueError, TypeError):
            logger.warning(
                "Ignoring invalid %s=%r (expected finite '<in_per_1k>/<out_per_1k>')",
                env_key,
                raw,
            )
    return MODEL_PRICING.get(model)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate the USD cost of one LLM call.

    Preconditions:
        - ``input_tokens >= 0`` and ``output_tokens >= 0``.
    Postconditions:
        - Returns a finite, non-negative float. An unknown model (no table entry
          and no ``LLM_PRICE_*`` override) returns ``0.0`` and logs at DEBUG — the
          cost is never guessed.
    """
    if input_tokens < 0 or output_tokens < 0:
        # Explicit validation (not ``assert``) so the contract holds under -O.
        raise ValueError(
            f"token counts must be non-negative, got input={input_tokens} output={output_tokens}"
        )
    price = _price_for_model(model)
    if price is None:
        logger.debug("No price for model %r; cost reported as $0", model)
        return 0.0
    cost = (input_tokens / 1000.0) * price.usd_per_1k_input + (
        output_tokens / 1000.0
    ) * price.usd_per_1k_output
    # Defensive: never return a non-finite cost even if a price slipped through.
    return cost if math.isfinite(cost) else 0.0


__all__ = ["ModelPrice", "MODEL_PRICING", "estimate_cost_usd"]
