"""Cross-check the Angular indicator model against the backend DSL registry.

The editor form in ``user-interface/src/app/models/investment.model.ts`` carries a
hand-maintained mirror of the backend ``_INDICATOR_PARAM_SPECS`` (the
``INDICATOR_SPECS`` record, the ``IndicatorName`` union, and the
``INDICATOR_NAME_OPTIONS`` list). Nothing enforces that the two stay in step, so
adding a backend indicator (or changing a bound / selector) without updating the
frontend silently ships an editor that can't express — or mis-validates — the new
indicator. This test parses the TypeScript source and asserts full parity:

* the indicator NAME set is identical across all four declarations, and
* every indicator's ``allow_source`` flag, param keys, required-ness, defaults,
  numeric bounds, and enum selectors match the backend spec.

It is a pure text cross-check (no Node / TS toolchain), and skips cleanly when the
frontend source is absent (e.g. a backend-only checkout).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from investment_team.strategy_lab.spec_dsl import _INDICATOR_PARAM_SPECS, IndicatorName

# Repo layout: backend/agents/investment_team/tests/<this file>. Four parents up
# from ``tests`` is the repository root that also holds ``user-interface``.
_MODEL_TS = (
    Path(__file__).resolve().parents[4]
    / "user-interface"
    / "src"
    / "app"
    / "models"
    / "investment.model.ts"
)


def _require_model_source() -> str:
    if not _MODEL_TS.is_file():
        pytest.skip(f"frontend model not present at {_MODEL_TS}")
    return _MODEL_TS.read_text(encoding="utf-8")


def _match_balanced(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Index of the ``close_ch`` that balances the ``open_ch`` at ``open_idx``.

    Precondition: ``text[open_idx] == open_ch``. Postcondition: returns ``j`` such
    that ``text[open_idx:j+1]`` is bracket-balanced. Ignores brackets — the TS here
    has no string containing ``{}``/``[]`` — which is sufficient for this source.
    """
    assert text[open_idx] == open_ch
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i
    raise AssertionError("unbalanced brackets in TS source")


def _ts_object_to_json(literal: str) -> Any:
    """Parse a JSON-like TS object/array literal into Python.

    Handles the subset used by ``investment.model.ts``: ``//`` line comments,
    unquoted identifier keys, single-quoted strings, and trailing commas.
    """
    text = re.sub(r"//[^\n]*", "", literal)  # strip line comments
    text = re.sub(r"([{\[,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', text)  # quote keys
    text = text.replace("'", '"')  # single -> double quotes
    text = re.sub(r",(\s*[}\]])", r"\1", text)  # drop trailing commas
    return json.loads(text)


def _extract_literal(source: str, anchor: str, open_ch: str, close_ch: str) -> Any:
    start = source.index(anchor) + len(anchor)
    open_idx = source.index(open_ch, start)
    close_idx = _match_balanced(source, open_idx, open_ch, close_ch)
    return _ts_object_to_json(source[open_idx : close_idx + 1])


def _frontend_name_union(source: str) -> set[str]:
    block = re.search(r"export type IndicatorName\s*=(.*?);", source, re.S).group(1)
    return set(re.findall(r"'([a-z_]+)'", block))


def _validator_kind_and_bounds(validator: Any) -> tuple[str, dict[str, Any]]:
    """Classify a ``_INDICATOR_PARAM_SPECS`` validator and recover its bounds.

    The DSL builds validators via ``_int_in`` / ``_float_gt`` / ``_one_of``; each is
    identifiable by its closure free-variables (and ``_one_of`` exposes ``.allowed``),
    so the bounds can be recovered without a second copy of the numbers.
    """
    if hasattr(validator, "allowed"):
        return "enum", {"options": sorted(validator.allowed)}
    free = validator.__code__.co_freevars
    cells = {name: cell.cell_contents for name, cell in zip(free, validator.__closure__ or ())}
    if set(free) == {"threshold"}:
        return "float", {"threshold": cells["threshold"]}
    if set(free) == {"lo", "hi"}:
        return "int", {"min": cells["lo"], "max": cells["hi"]}
    raise AssertionError(f"unrecognised validator with free vars {free}")


def _backend_params(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, validator in spec["required"].items():
        kind, meta = _validator_kind_and_bounds(validator)
        out[key] = {"required": True, "kind": kind, **meta}
    for key, (default, validator) in spec["optional"].items():
        kind, meta = _validator_kind_and_bounds(validator)
        out[key] = {"required": False, "default": default, "kind": kind, **meta}
    return out


def _frontend_specs(source: str) -> dict[str, dict[str, Any]]:
    return _extract_literal(source, "export const INDICATOR_SPECS", "{", "}")


def test_indicator_name_sets_match_across_layers() -> None:
    source = _require_model_source()
    backend = set(IndicatorName.__args__)
    assert backend == set(_INDICATOR_PARAM_SPECS), "backend union vs registry drift"

    union = _frontend_name_union(source)
    options = set(_extract_literal(source, "INDICATOR_NAME_OPTIONS: IndicatorName[] =", "[", "]"))
    specs = set(_frontend_specs(source))
    assert backend == union, f"IndicatorName union drift: {backend ^ union}"
    assert backend == options, f"INDICATOR_NAME_OPTIONS drift: {backend ^ options}"
    assert backend == specs, f"INDICATOR_SPECS key drift: {backend ^ specs}"


def test_indicator_param_specs_match_backend() -> None:
    source = _require_model_source()
    fe_specs = _frontend_specs(source)

    for name, be_spec in _INDICATOR_PARAM_SPECS.items():
        fe = fe_specs[name]
        assert fe["name"] == name
        assert fe["allowSource"] == be_spec["allow_source"], f"{name}: allow_source drift"

        be_params = _backend_params(be_spec)
        fe_params = {p["key"]: p for p in fe["params"]}
        assert set(fe_params) == set(be_params), (
            f"{name}: param-key drift {set(fe_params) ^ set(be_params)}"
        )

        for key, be_p in be_params.items():
            fe_p = fe_params[key]
            assert fe_p["required"] == be_p["required"], f"{name}.{key}: required drift"
            assert fe_p["kind"] == be_p["kind"], f"{name}.{key}: kind drift"
            if not be_p["required"]:
                assert fe_p.get("default") == be_p["default"], f"{name}.{key}: default drift"
            if be_p["kind"] == "int":
                assert fe_p["min"] == be_p["min"], f"{name}.{key}: min drift"
                assert fe_p["max"] == be_p["max"], f"{name}.{key}: max drift"
            elif be_p["kind"] == "float":
                # The backend bound is ``> threshold`` (an open bound); the form uses a
                # small positive floor as its representable proxy. Assert the proxy is a
                # value the backend accepts and that the field carries no upper bound.
                assert fe_p["min"] > be_p["threshold"], f"{name}.{key}: float floor drift"
                assert "max" not in fe_p, f"{name}.{key}: float should be unbounded above"
            elif be_p["kind"] == "enum":
                assert sorted(fe_p["options"]) == be_p["options"], f"{name}.{key}: enum drift"
