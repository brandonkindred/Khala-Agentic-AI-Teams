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

    Preconditions: ``literal`` is a balanced TS object/array using the subset in
    ``investment.model.ts`` — ``//`` line comments, unquoted identifier keys,
    single-quoted strings, and trailing commas. String contents may contain any
    character (including ``//``, ``'``, ``"``).
    Postconditions: returns the parsed Python value. String literals are converted to
    JSON with a single left-to-right scan that tracks string state, so comment
    stripping and key quoting never touch characters inside a string (a value
    containing ``//`` or an apostrophe is preserved verbatim). Raises
    ``json.JSONDecodeError`` if the (transformed) literal is not valid JSON.
    """
    out: list[str] = []
    i, n = 0, len(literal)
    quote = ""  # "" outside a string, else the opening delimiter (' or ")
    while i < n:
        ch = literal[i]
        if quote:
            if ch == "\\" and i + 1 < n:  # normalise escapes for JSON
                nxt = literal[i + 1]
                if nxt == "'":  # apostrophes need no escape in JSON
                    out.append("'")
                elif nxt == '"':
                    out.append('\\"')
                else:  # keep \n \t \\ \uXXXX etc. verbatim
                    out.append(literal[i : i + 2])
                i += 2
                continue
            if ch == quote:  # closing delimiter -> JSON double quote
                out.append('"')
                quote = ""
            elif ch == '"':  # a literal " inside a '...' string must be escaped
                out.append('\\"')
            else:
                out.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):  # opening delimiter (either style)
            out.append('"')
            quote = ch
            i += 1
            continue
        if ch == "/" and i + 1 < n and literal[i + 1] == "/":  # line comment (outside strings)
            while i < n and literal[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    # Keys are now the only bare identifiers followed by ``:``; string values are
    # already double-quoted, so quoting keys can't touch string contents.
    text = re.sub(r"([{\[,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', "".join(out))
    text = re.sub(r",(\s*[}\]])", r"\1", text)  # drop trailing commas
    return json.loads(text)


def _extract_literal(source: str, anchor: str, open_ch: str, close_ch: str) -> Any:
    """Parse the ``open_ch``…``close_ch`` literal that follows ``anchor`` in ``source``.

    Preconditions: ``anchor`` occurs in ``source`` (its first occurrence is used), and
    the next ``open_ch`` after it opens a balanced, JSON-like TS literal.
    Postconditions: returns the parsed Python value of that literal (via
    :func:`_ts_object_to_json`). Raises ``ValueError`` if ``anchor``/``open_ch`` is
    absent and ``AssertionError`` if the brackets never balance.
    """
    start = source.index(anchor) + len(anchor)
    open_idx = source.index(open_ch, start)
    close_idx = _match_balanced(source, open_idx, open_ch, close_ch)
    return _ts_object_to_json(source[open_idx : close_idx + 1])


def _frontend_name_union(source: str) -> set[str]:
    """Indicator names declared in the ``export type IndicatorName = 'a' | 'b' | …`` union.

    Preconditions: ``source`` contains the ``IndicatorName`` type-union declaration.
    Postconditions: the set of quoted member names. The name pattern allows digits so a
    numeric-suffixed indicator (e.g. ``ema200``) is captured whole rather than truncated
    — keeping this parser consistent with the digit-tolerant JSON parse of
    ``INDICATOR_NAME_OPTIONS``.
    """
    block = re.search(r"export type IndicatorName\s*=(.*?);", source, re.S).group(1)
    return set(re.findall(r"'([a-z0-9_]+)'", block))


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
    """Normalise one ``_INDICATOR_PARAM_SPECS`` entry into a per-key comparison dict.

    Preconditions: ``spec`` is a registry entry with ``required``/``optional`` maps of
    param-name → validator (``optional`` values are ``(default, validator)`` tuples).
    Postconditions: ``{key: {"required": bool, "kind": str, ...bounds, ["default"]}}``
    with bounds recovered via :func:`_validator_kind_and_bounds`, ready to compare
    field-by-field against the frontend param spec.
    """
    out: dict[str, dict[str, Any]] = {}
    for key, validator in spec["required"].items():
        kind, meta = _validator_kind_and_bounds(validator)
        out[key] = {"required": True, "kind": kind, **meta}
    for key, (default, validator) in spec["optional"].items():
        kind, meta = _validator_kind_and_bounds(validator)
        out[key] = {"required": False, "default": default, "kind": kind, **meta}
    return out


def _frontend_specs(source: str) -> dict[str, dict[str, Any]]:
    """Parse the frontend ``INDICATOR_SPECS`` record into a Python dict.

    Preconditions: ``source`` is the ``investment.model.ts`` text and contains the
    ``export const INDICATOR_SPECS`` declaration.
    Postconditions: ``{indicator_name: {"name", "allowSource", "params": [...]}}`` as
    declared in the TypeScript, keyed by indicator name.
    """
    return _extract_literal(source, "export const INDICATOR_SPECS", "{", "}")


def test_ts_object_parser_is_string_aware() -> None:
    # The literal parser must not corrupt string VALUES: ``//`` inside a string is not
    # a comment, an apostrophe (raw, or escaped in a single-quoted string, or inside a
    # double-quoted string) is not a delimiter, and trailing commas are dropped. These
    # guard the parity check against a future benign edit to investment.model.ts.
    assert _ts_object_to_json("{ foo: 'a//b', bar: ['x', 'y'], }") == {
        "foo": "a//b",
        "bar": ["x", "y"],
    }
    assert _ts_object_to_json("{ label: 'd\\'oh', n: 2 }") == {"label": "d'oh", "n": 2}
    assert _ts_object_to_json('{ label: "don\'t" }') == {"label": "don't"}
    assert _ts_object_to_json("{ a: 1 } // trailing comment") == {"a": 1}


def test_frontend_name_union_allows_digits() -> None:
    # The union regex must capture digit-bearing names whole (consistent with the
    # digit-tolerant JSON parse of INDICATOR_NAME_OPTIONS), or it would report phantom
    # drift for an indicator like ``ema200``.
    block = "export type IndicatorName =\n  | 'ema200'\n  | 'sma';\n"
    assert _frontend_name_union(block) == {"ema200", "sma"}


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
