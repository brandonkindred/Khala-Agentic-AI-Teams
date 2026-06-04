"""Pure-Python tests for the rules predicate DSL (Step 5).

No Postgres needed — the DSL engine has no DB dependency, so these always run.
"""

from __future__ import annotations

import pytest

from agent_cognition.rules.predicate import (
    Predicate,
    PredicateError,
    evaluate,
    is_valid_predicate,
    parse_predicate,
    validate_predicate,
)


def _eval(pred_dict: dict, root: dict) -> tuple[bool, str | None]:
    return evaluate(parse_predicate(pred_dict), root)


# ---------------------------------------------------------------------------
# Parsing — valid constructs
# ---------------------------------------------------------------------------
def test_parse_returns_predicate() -> None:
    pred = parse_predicate(
        {"phase": "precondition", "check": {"op": "==", "path": "input.x", "value": 1}}
    )
    assert isinstance(pred, Predicate)
    assert pred.phase == "precondition"


def test_parse_valid_constructs() -> None:
    parse_predicate({"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": "shell"}})
    parse_predicate({"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": ["a", "b"]}})
    parse_predicate(
        {
            "phase": "precondition",
            "check": {
                "op": "any",
                "of": [
                    {"op": "<", "path": "input.x", "value": 1},
                    {"op": "not", "of": [{"op": "in", "path": "input.y", "value": [1, 2]}]},
                ],
            },
        }
    )


# ---------------------------------------------------------------------------
# Parsing — rejected constructs (no eval, raises PredicateError)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        [],  # not a dict
        {},  # missing phase
        {"phase": "bogus", "check": {"op": "==", "path": "input.x", "value": 1}},  # unknown phase
        {
            "phase": "precondition",
            "check": {"op": "==", "path": "input.x", "value": 1},
            "extra": 1,
        },  # extra top key
        {"phase": "precondition"},  # missing check
        {"phase": "precondition", "check": []},  # node not a dict
        {"phase": "precondition", "check": {"path": "input.x", "value": 1}},  # missing op
        {
            "phase": "precondition",
            "check": {"op": 123, "path": "input.x", "value": 1},
        },  # op not str
        {
            "phase": "precondition",
            "check": {"op": "regex", "path": "input.x", "value": 1},
        },  # unknown op
        {
            "phase": "precondition",
            "check": {"op": "forbid_tool", "tool_id": "x"},
        },  # forbid_tool wrong phase
        {"phase": "precondition", "check": {"op": "==", "value": 1}},  # missing path
        {"phase": "precondition", "check": {"op": "==", "path": "", "value": 1}},  # empty path
        {
            "phase": "precondition",
            "check": {"op": "==", "path": "a..b", "value": 1},
        },  # empty segment
        {"phase": "precondition", "check": {"op": "==", "path": "input.x"}},  # missing value
        {
            "phase": "precondition",
            "check": {"op": "in", "path": "input.x", "value": "no"},
        },  # in non-array
        {
            "phase": "precondition",
            "check": {"op": "==", "path": "input.x", "value": [1, 2]},
        },  # scalar op list
        {
            "phase": "precondition",
            "check": {"op": "==", "path": "input.x", "value": {"a": 1}},
        },  # scalar op dict
        {"phase": "precondition", "check": {"op": "all", "of": []}},  # empty of
        {"phase": "precondition", "check": {"op": "all", "of": "x"}},  # of not a list
        {  # not with two children
            "phase": "precondition",
            "check": {
                "op": "not",
                "of": [
                    {"op": "==", "path": "a", "value": 1},
                    {"op": "==", "path": "b", "value": 2},
                ],
            },
        },
        {
            "phase": "tool_gate",
            "check": {"op": "forbid_tool", "tool_id": 123},
        },  # tool_id not str/list
        {"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": ""}},  # empty str
        {"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": []}},  # empty list
        {
            "phase": "tool_gate",
            "check": {"op": "forbid_tool", "tool_id": ["ok", ""]},
        },  # empty in list
        {
            "phase": "tool_gate",
            "check": {"op": "forbid_tool", "tool_id": ["ok", 1]},
        },  # non-str in list
        {
            "phase": "tool_gate",
            "check": {"op": "forbid_tool", "tool_id": "x", "extra": 1},
        },  # extra key
        {
            "phase": "precondition",
            "check": {"op": "==", "path": "input.x", "value": 1, "extra": 1},
        },  # extra key
        {  # extra key on composite
            "phase": "precondition",
            "check": {"op": "all", "of": [{"op": "==", "path": "a", "value": 1}], "extra": 1},
        },
    ],
)
def test_parse_rejects_invalid(bad: object) -> None:
    with pytest.raises(PredicateError):
        parse_predicate(bad)


# ---------------------------------------------------------------------------
# Evaluation — comparisons
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "op,value,actual,expected",
    [
        ("<=", 0.7, 0.5, True),
        ("<=", 0.7, 0.7, True),
        ("<=", 0.7, 0.8, False),
        ("<", 5, 4, True),
        ("<", 5, 5, False),
        (">", 5, 6, True),
        (">", 5, 5, False),
        (">=", 5, 5, True),
        (">=", 5, 4, False),
    ],
)
def test_numeric_comparison(op: str, value: float, actual: float, expected: bool) -> None:
    pred = {"phase": "precondition", "check": {"op": op, "path": "input.x", "value": value}}
    holds, reason = _eval(pred, {"input": {"x": actual}})
    assert holds is expected
    assert (reason is None) is expected


def test_numeric_non_numeric_operand_fails_closed() -> None:
    pred = {"phase": "precondition", "check": {"op": "<=", "path": "input.x", "value": 5}}
    holds, reason = _eval(pred, {"input": {"x": "big"}})
    assert holds is False
    assert reason is not None and "numeric" in reason


def test_numeric_bool_actual_fails_closed() -> None:
    # bool is an int subclass but must not satisfy an ordered comparison.
    pred = {"phase": "precondition", "check": {"op": ">=", "path": "input.x", "value": 0}}
    holds, _reason = _eval(pred, {"input": {"x": True}})
    assert holds is False


def test_eq_neq() -> None:
    assert (
        _eval(
            {"phase": "postcondition", "check": {"op": "==", "path": "output.s", "value": "ok"}},
            {"output": {"s": "ok"}},
        )[0]
        is True
    )
    assert (
        _eval(
            {"phase": "postcondition", "check": {"op": "==", "path": "output.s", "value": "ok"}},
            {"output": {"s": "x"}},
        )[0]
        is False
    )
    assert (
        _eval(
            {"phase": "postcondition", "check": {"op": "!=", "path": "output.s", "value": "x"}},
            {"output": {"s": "ok"}},
        )[0]
        is True
    )
    # explicit None value compares against a present None
    assert (
        _eval(
            {"phase": "postcondition", "check": {"op": "==", "path": "output.s", "value": None}},
            {"output": {"s": None}},
        )[0]
        is True
    )


def test_in_membership() -> None:
    pred = {
        "phase": "precondition",
        "check": {"op": "in", "path": "input.env", "value": ["dev", "staging"]},
    }
    assert _eval(pred, {"input": {"env": "dev"}})[0] is True
    holds, reason = _eval(pred, {"input": {"env": "prod"}})
    assert holds is False and reason is not None and "not in" in reason


def test_missing_path_fails_closed() -> None:
    holds, reason = _eval(
        {"phase": "precondition", "check": {"op": "==", "path": "input.x", "value": 1}},
        {"input": {}},
    )
    assert holds is False and reason is not None and "missing" in reason
    # walking through a non-dict intermediate is also MISSING
    holds2, _ = _eval(
        {"phase": "precondition", "check": {"op": "==", "path": "input.a.b", "value": 1}},
        {"input": {"a": 5}},
    )
    assert holds2 is False


# ---------------------------------------------------------------------------
# Evaluation — forbid_tool
# ---------------------------------------------------------------------------
def test_forbid_tool_string() -> None:
    pred = {"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": "shell"}}
    blocked, reason = _eval(pred, {"tool_id": "shell", "args": {}})
    assert blocked is False and reason is not None and "forbidden" in reason
    assert _eval(pred, {"tool_id": "http", "args": {}})[0] is True


def test_forbid_tool_list() -> None:
    pred = {"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": ["shell", "rm"]}}
    assert _eval(pred, {"tool_id": "rm", "args": {}})[0] is False
    assert _eval(pred, {"tool_id": "ls", "args": {}})[0] is True


def test_tool_gate_arg_conditioned_not_all() -> None:
    # Allow http_api unless method == DELETE — expressed with comparison leaves
    # under not(all(...)), not a forbid_tool/comparison mix.
    pred = {
        "phase": "tool_gate",
        "check": {
            "op": "not",
            "of": [
                {
                    "op": "all",
                    "of": [
                        {"op": "==", "path": "tool_id", "value": "http_api"},
                        {"op": "==", "path": "args.method", "value": "DELETE"},
                    ],
                }
            ],
        },
    }
    assert _eval(pred, {"tool_id": "http_api", "args": {"method": "DELETE"}})[0] is False
    assert _eval(pred, {"tool_id": "http_api", "args": {"method": "GET"}})[0] is True
    assert _eval(pred, {"tool_id": "git", "args": {"method": "DELETE"}})[0] is True


# ---------------------------------------------------------------------------
# Evaluation — composites
# ---------------------------------------------------------------------------
def test_all() -> None:
    pred = {
        "phase": "precondition",
        "check": {
            "op": "all",
            "of": [
                {"op": "==", "path": "input.a", "value": 1},
                {"op": "==", "path": "input.b", "value": 2},
            ],
        },
    }
    assert _eval(pred, {"input": {"a": 1, "b": 2}})[0] is True
    holds, reason = _eval(pred, {"input": {"a": 1, "b": 3}})
    assert holds is False and reason is not None


def test_any() -> None:
    pred = {
        "phase": "precondition",
        "check": {
            "op": "any",
            "of": [
                {"op": "==", "path": "input.a", "value": 1},
                {"op": "==", "path": "input.b", "value": 2},
            ],
        },
    }
    assert _eval(pred, {"input": {"a": 9, "b": 2}})[0] is True
    holds, reason = _eval(pred, {"input": {"a": 9, "b": 9}})
    assert holds is False and reason is not None and "no alternative" in reason


def test_not() -> None:
    pred = {
        "phase": "precondition",
        "check": {"op": "not", "of": [{"op": "==", "path": "input.a", "value": 1}]},
    }
    assert _eval(pred, {"input": {"a": 2}})[0] is True
    holds, reason = _eval(pred, {"input": {"a": 1}})
    assert holds is False and reason is not None and "negated" in reason


# ---------------------------------------------------------------------------
# API guards
# ---------------------------------------------------------------------------
def test_evaluate_requires_parsed_predicate() -> None:
    with pytest.raises(TypeError):
        evaluate({"phase": "precondition", "check": {}}, {})  # type: ignore[arg-type]


def test_validate_and_is_valid() -> None:
    good = {"phase": "precondition", "check": {"op": "==", "path": "input.x", "value": 1}}
    assert validate_predicate(good) is None
    assert is_valid_predicate(good) is True
    bad = {"phase": "nope", "check": {}}
    with pytest.raises(PredicateError):
        validate_predicate(bad)
    assert is_valid_predicate(bad) is False


# ---------------------------------------------------------------------------
# Missing-path semantics are per-operator (a missing value is a distinct value)
# ---------------------------------------------------------------------------
def test_missing_path_per_operator() -> None:
    root = {"output": {}}  # 'error'/'status' absent
    # != allows when the path is absent (the success case)
    assert (
        _eval(
            {
                "phase": "postcondition",
                "check": {"op": "!=", "path": "output.error", "value": "fatal"},
            },
            root,
        )[0]
        is True
    )
    # == / in block when the required path is absent
    assert (
        _eval(
            {
                "phase": "postcondition",
                "check": {"op": "==", "path": "output.status", "value": "ok"},
            },
            root,
        )[0]
        is False
    )
    assert (
        _eval(
            {
                "phase": "postcondition",
                "check": {"op": "in", "path": "output.status", "value": ["ok"]},
            },
            root,
        )[0]
        is False
    )


def test_missing_path_composes_through_not() -> None:
    # 'block when approved == true' must ALLOW when 'approved' is absent — the
    # fail-closed-on-missing of the old design flipped to allow under not; now
    # the inner == is a concrete False so not(False) == allow, consistently.
    pred = {
        "phase": "postcondition",
        "check": {"op": "not", "of": [{"op": "==", "path": "output.approved", "value": True}]},
    }
    assert _eval(pred, {"output": {}})[0] is True
    assert _eval(pred, {"output": {"approved": True}})[0] is False


def test_strict_bool_int_equality() -> None:
    # == / != / in never coerce bool <-> int (True is not 1).
    assert (
        _eval(
            {"phase": "tool_gate", "check": {"op": "==", "path": "args.dry_run", "value": True}},
            {"tool_id": "t", "args": {"dry_run": 1}},
        )[0]
        is False
    )
    assert (
        _eval(
            {"phase": "tool_gate", "check": {"op": "==", "path": "args.dry_run", "value": True}},
            {"tool_id": "t", "args": {"dry_run": True}},
        )[0]
        is True
    )
    assert (
        _eval(
            {"phase": "precondition", "check": {"op": "in", "path": "input.n", "value": [1, 2]}},
            {"input": {"n": True}},
        )[0]
        is False
    )


def test_forbid_tool_non_string_tool_id_never_raises() -> None:
    # Public evaluate must not raise on an unhashable/non-string tool_id; a
    # non-string id simply cannot match a forbidden string id, so it allows.
    pred = {"phase": "tool_gate", "check": {"op": "forbid_tool", "tool_id": "shell"}}
    assert _eval(pred, {"tool_id": ["shell"], "args": {}})[0] is True
    assert _eval(pred, {"tool_id": None, "args": {}})[0] is True


def test_eval_does_not_repr_on_success_path() -> None:
    # A passing comparison must not call repr(actual); a value whose __repr__
    # raises must not break evaluation (repr is only built for failure reasons).
    class _BoomRepr:
        def __eq__(self, other: object) -> bool:
            return True

        def __repr__(self) -> str:
            raise RuntimeError("boom")

    pred = {"phase": "precondition", "check": {"op": "==", "path": "input.x", "value": "v"}}
    assert _eval(pred, {"input": {"x": _BoomRepr()}}) == (True, None)
