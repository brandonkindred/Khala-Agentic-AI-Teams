"""Shared prompt-construction helpers for the Strategy Lab agent suite.

Two blocks of ``.format()`` context were duplicated verbatim across the
spec-authoring agents in this package: a "prior attempts" renderer
(``refinement.py``, ``alignment.py``, ``zero_trade_repair.py``) and the
seven-field ``StrategySpec`` context block repeated at every agent that
shows the LLM the current spec (``refinement.py``, ``design_review.py``,
``code_synthesis.py``, ``zero_trade_repair.py``, ``alignment.py``,
``analysis.py``). Both are extracted here so a wording or computation
change lands in one place instead of silently drifting across call sites.

Invariants:
  * Both helpers are pure — no I/O, no mutation of their arguments.
  * ``spec_prompt_fields``'s non-defensive branch preserves the exact
    attribute-access formula each call site used before extraction; it
    adds no new tolerance for missing/malformed input.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..spec_dsl import format_rules_for_prompt, format_sizing_rule


def render_prior_attempts(prior_attempts: Optional[List[str]]) -> str:
    """Render a numbered list of prior-attempt summaries for a prompt.

    Preconditions: ``prior_attempts`` is ``None`` or a list. Elements are
    only ever interpolated into an f-string, never inspected, so
    non-string elements are tolerated (stringified), not rejected.
    Postconditions: returns ``"None yet."`` when ``prior_attempts`` is
    falsy (``None`` or ``[]``); otherwise one line per entry, 1-indexed
    and in input order, formatted ``"  Round {n}: {entry}"`` and joined
    with ``"\\n"``.
    """
    return (
        "None yet."
        if not prior_attempts
        else "\n".join(f"  Round {i + 1}: {a}" for i, a in enumerate(prior_attempts))
    )


def spec_prompt_fields(spec: Any, *, defensive: bool = False) -> Dict[str, str]:
    """Render the seven ``StrategySpec`` fields shared by every agent prompt.

    Returns exactly the keys ``asset_class``, ``hypothesis``,
    ``signal_definition``, ``entry_rules``, ``exit_rules``,
    ``sizing_rules``, ``risk_limits`` — no more, no fewer. Callers spread
    the result into their own ``.format(**spec_prompt_fields(spec), ...)``
    call alongside whatever extra kwargs their template needs (e.g.
    ``target_symbols``, ``timeframe``): ``str.format`` silently ignores
    kwargs a template doesn't reference, so returning this fixed superset
    is safe even for templates that use only some of the seven keys.

    Preconditions: with ``defensive=False`` (the default), ``spec`` is a
    fully constructed ``StrategySpec``. With ``defensive=True``, ``spec``
    may be anything — including ``None`` — since every access goes
    through ``getattr``/``hasattr`` with a fallback.
    Postconditions:
      * ``defensive=False``: fields are read directly off ``spec`` with
        no added tolerance — a missing attribute raises
        ``AttributeError``, exactly as the pre-extraction call sites did.
      * ``defensive=True``: every field falls back independently rather
        than raising. ``risk_limits`` is NOT a simple "missing vs
        present" fallback: a ``spec`` with no ``risk_limits`` attribute
        at all renders ``""``, but a ``spec`` whose ``risk_limits`` is
        explicitly ``None`` renders the 4-character string ``"None"`` —
        two independent ``getattr`` calls with different defaults,
        inherited verbatim from the original ``alignment.py`` call site
        rather than "fixed" here. A non-``None`` ``sizing`` value that
        ``format_sizing_rule`` does not recognise still raises
        ``TypeError`` even under ``defensive=True`` — this mode adds
        tolerance for missing/``None`` values, not for malformed ones.
    """
    if defensive:
        return {
            "asset_class": getattr(spec, "asset_class", "?"),
            "hypothesis": getattr(spec, "hypothesis", "?"),
            "signal_definition": getattr(spec, "signal_definition", "?"),
            "entry_rules": format_rules_for_prompt(getattr(spec, "entry_rules", []) or []),
            "exit_rules": format_rules_for_prompt(getattr(spec, "exit_rules", []) or []),
            "sizing_rules": (
                format_sizing_rule(spec.sizing)
                if getattr(spec, "sizing", None) is not None
                else "(none)"
            ),
            "risk_limits": (
                spec.risk_limits.model_dump_json()
                if hasattr(getattr(spec, "risk_limits", None), "model_dump_json")
                else str(getattr(spec, "risk_limits", ""))
            ),
        }
    return {
        "asset_class": spec.asset_class,
        "hypothesis": spec.hypothesis,
        "signal_definition": spec.signal_definition,
        "entry_rules": format_rules_for_prompt(spec.entry_rules),
        "exit_rules": format_rules_for_prompt(spec.exit_rules),
        "sizing_rules": format_sizing_rule(spec.sizing),
        "risk_limits": spec.risk_limits.model_dump_json(),
    }


__all__ = ["render_prior_attempts", "spec_prompt_fields"]
