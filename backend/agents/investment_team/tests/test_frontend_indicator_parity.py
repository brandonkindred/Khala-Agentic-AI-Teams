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


_STRING_PLACEHOLDER = re.compile("\x00(\\d+)\x00")

# One token = a block comment, a line comment, or a single/double-quoted string
# (``\\.`` inside the string classes swallows any escaped quote, so a quote never
# closes early). Ordering matters only between the two comment forms and strings:
# scanning left-to-right, a comment marker inside a string is consumed as part of the
# string token (the alternation is tried at the opening quote, not mid-string), so
# ``//`` / ``/*`` inside a value are never mistaken for comments.
_TS_TOKEN = re.compile(
    r"/\*.*?\*/"  # block comment (JSDoc), possibly multi-line
    r"|//[^\n]*"  # line comment
    r"|'(?:\\.|[^'\\])*'"  # single-quoted string
    r'|"(?:\\.|[^"\\])*"',  # double-quoted string
    re.S,
)

# A backslash escape inside a string: capture the escaped char so the caller can decide
# whether to unescape it (a quote/backslash) or keep it verbatim (anything else).
_TS_STRING_ESCAPE = re.compile(r"\\(.)", re.S)


def _unescape_ts_string(inner: str) -> str:
    """Decode the content between a TS string's quotes into its Python value.

    Preconditions: ``inner`` is the text between the delimiters of a single/double-quoted
    string (delimiters already stripped).
    Postconditions: only the escapes the model file's quoted values can contain — an
    escaped quote (``\\'`` / ``\\"``) or backslash (``\\\\``) — are unescaped; every other
    ``\\x`` sequence is preserved VERBATIM (backslash kept). This is deliberately scoped
    to the file's vocabulary of plain ASCII identifiers, which carry no escapes: it never
    executes the string (so an ES6 ``\\u{...}`` or an unknown escape can't crash the
    parse), and it never silently drops a backslash to produce a plausible-but-wrong
    value — an exotic future escape stays literal and surfaces as a LOUD parity mismatch.
    """
    return _TS_STRING_ESCAPE.sub(
        lambda m: m.group(1) if m.group(1) in "'\"\\" else m.group(0), inner
    )


def _mask_strings(text: str) -> tuple[str, list[str]]:
    """Replace string literals with inert placeholders and strip comments.

    Preconditions: ``text`` begins at a structural position (not inside a string) and
    uses the ``investment.model.ts`` subset — single/double-quoted strings and
    ``//`` / ``/* */`` comments.
    Postconditions: ``(masked, strings)`` where every string literal is replaced by a
    placeholder ``"\\x00<i>\\x00"`` (which contains no JSON-structural character — no
    quote, bracket, brace, comma or colon) and every comment is removed; ``strings[i]``
    is the JSON encoding (via :func:`json.dumps`) of the i-th literal decoded by
    :func:`_unescape_ts_string`. Because all string content is masked out, downstream
    structural transforms (bracket matching, key quoting, trailing-comma removal) can
    never touch characters that live inside a string.
    """
    strings: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith("/"):  # a // or /* */ comment -> drop it
            return ""
        value = _unescape_ts_string(token[1:-1])  # strip delimiters, decode escapes
        strings.append(json.dumps(value))  # string literal -> placeholder
        return f"\x00{len(strings) - 1}\x00"

    return _TS_TOKEN.sub(_replace, text), strings


def _match_balanced(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Index of the ``close_ch`` that balances the ``open_ch`` at ``open_idx``.

    Preconditions: ``text[open_idx] == open_ch`` and ``text`` is string-masked (see
    :func:`_mask_strings`), so no bracket lives inside a string literal.
    Postconditions: returns ``j`` such that ``text[open_idx:j+1]`` is bracket-balanced.
    Raises ``AssertionError`` if the brackets never balance.
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


def _masked_to_json(masked_body: str, strings: list[str]) -> Any:
    """Quote keys, drop trailing commas, restore masked strings, and parse as JSON.

    Preconditions: ``masked_body`` is a balanced, string-masked object/array literal
    (from :func:`_mask_strings`) and ``strings`` its captured JSON string encodings.
    Postconditions: returns the parsed Python value. Key quoting and trailing-comma
    removal operate only on masked (structural) text, so string contents — the
    placeholders — are inert to both regexes and are substituted back verbatim.
    """
    body = re.sub(r"([{\[,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', masked_body)  # quote keys
    body = re.sub(r",(\s*[}\]])", r"\1", body)  # drop trailing commas
    body = _STRING_PLACEHOLDER.sub(lambda m: strings[int(m.group(1))], body)  # restore strings
    return json.loads(body)


def _ts_object_to_json(literal: str) -> Any:
    """Parse a JSON-like TS object/array literal into Python.

    Preconditions: ``literal`` is a balanced TS object/array using the subset in
    ``investment.model.ts`` — ``//`` and ``/* */`` comments, unquoted identifier keys,
    single/double-quoted strings, and trailing commas. String contents may contain any
    character (including ``//``, quotes, brackets, commas, colons).
    Postconditions: returns the parsed Python value. String literals are masked out
    first (:func:`_mask_strings`), so the structural transforms never reinterpret a
    character inside a string as syntax — a value containing ``//``, ``, key:``, or a
    bracket is preserved verbatim. Raises ``json.JSONDecodeError`` if the (transformed)
    literal is not valid JSON.
    """
    masked, strings = _mask_strings(literal)
    return _masked_to_json(masked, strings)


def _extract_literal(source: str, anchor: str, open_ch: str, close_ch: str) -> Any:
    """Parse the ``open_ch``…``close_ch`` literal that follows ``anchor`` in ``source``.

    Preconditions: ``anchor`` occurs in ``source`` (its first occurrence is used), and
    the next ``open_ch`` after it opens a balanced, JSON-like TS literal.
    Postconditions: returns the parsed Python value of that literal. String literals are
    masked before bracket matching, so a brace/bracket inside a string value cannot
    mis-terminate the slice. Raises ``ValueError`` if ``anchor``/``open_ch`` is absent
    and ``AssertionError`` if the brackets never balance.
    """
    start = source.index(anchor) + len(anchor)
    open_idx = source.index(open_ch, start)
    masked, strings = _mask_strings(source[open_idx:])
    close_idx = _match_balanced(masked, 0, open_ch, close_ch)
    return _masked_to_json(masked[: close_idx + 1], strings)


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
    # A string VALUE must be preserved verbatim: characters that are structural OUTSIDE
    # a string (``//`` comment, ``,``/``:`` delimiters, ``[]``/``{}`` brackets, quotes)
    # carry no syntactic meaning inside one. These guard the parity check against a
    # future benign edit to investment.model.ts.
    assert _ts_object_to_json("{ foo: 'a//b', bar: ['x', 'y'], }") == {
        "foo": "a//b",
        "bar": ["x", "y"],
    }
    # Comma / colon / brackets inside a string must NOT be treated as structure (the
    # bug the string-aware rewrite fixes): a naive trailing-comma or key-quoting regex
    # over the whole text would corrupt these.
    assert _ts_object_to_json("{ label: 'arr [1,]' }") == {"label": "arr [1,]"}
    assert _ts_object_to_json("{ label: 'min, max: n/a' }") == {"label": "min, max: n/a"}
    assert _ts_object_to_json("{ label: 'range [2,400]', n: 3 }") == {
        "label": "range [2,400]",
        "n": 3,
    }
    # Quote handling: escaped apostrophe in a single-quoted string, apostrophe inside a
    # double-quoted string, and an escaped double-quote.
    assert _ts_object_to_json("{ label: 'd\\'oh', n: 2 }") == {"label": "d'oh", "n": 2}
    assert _ts_object_to_json('{ label: "don\'t" }') == {"label": "don't"}
    assert _ts_object_to_json('{ label: "say \\"hi\\"" }') == {"label": 'say "hi"'}
    # Only quote/backslash escapes are unescaped; any OTHER escape is preserved verbatim
    # (backslash kept) — the compared vocabulary has none, and keeping it literal means an
    # exotic future escape fails LOUD (a parity mismatch) rather than crashing the parse
    # or silently decoding to a plausible-but-wrong value.
    assert _ts_object_to_json("{ label: 'a\\tb' }") == {"label": "a\\tb"}
    assert _ts_object_to_json("{ label: 'A is \\u0041' }") == {"label": "A is \\u0041"}
    # ES6 code-point / unknown escapes must NOT crash the parser (ast.literal_eval did).
    assert _ts_object_to_json("{ label: 'emoji \\u{1F4C8}' }") == {"label": "emoji \\u{1F4C8}"}
    # Line AND block comments (outside strings) are dropped; a comment marker inside a
    # string is preserved.
    assert _ts_object_to_json("{ a: 1 } // trailing comment") == {"a": 1}
    assert _ts_object_to_json("{ a: 1, // inline\n b: 2, }") == {"a": 1, "b": 2}
    assert _ts_object_to_json("{ a: 1, /* note, with: commas */ b: 2 }") == {"a": 1, "b": 2}
    assert _ts_object_to_json("{ /**\n * doc\n */ a: 1 }") == {"a": 1}
    assert _ts_object_to_json("{ label: 'a/*not*/b' }") == {"label": "a/*not*/b"}


def test_match_balanced_rejects_unbalanced_brackets() -> None:
    # The bracket matcher must raise (not silently return a wrong index) on input whose
    # brackets never close — guards the slice extraction in _extract_literal.
    with pytest.raises(AssertionError, match="unbalanced"):
        _match_balanced("{ a: 1 ", 0, "{", "}")


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
