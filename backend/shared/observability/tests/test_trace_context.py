"""Unit tests for shared.observability.trace_context."""

from __future__ import annotations

import pytest

from shared.observability.trace_context import bind_trace_id, current_trace_id, new_trace_id


def test_new_trace_id_is_nonempty_and_unique() -> None:
    a = new_trace_id()
    b = new_trace_id()
    assert a and isinstance(a, str)
    assert a != b


def test_current_trace_id_defaults_to_empty() -> None:
    assert current_trace_id() == ""


def test_bind_trace_id_sets_and_restores() -> None:
    assert current_trace_id() == ""
    with bind_trace_id("trace-1"):
        assert current_trace_id() == "trace-1"
    assert current_trace_id() == ""


def test_bind_trace_id_restores_on_exception() -> None:
    with pytest.raises(RuntimeError):
        with bind_trace_id("trace-err"):
            assert current_trace_id() == "trace-err"
            raise RuntimeError("boom")
    assert current_trace_id() == ""


def test_bind_trace_id_rejects_empty_string() -> None:
    with pytest.raises(ValueError):
        with bind_trace_id(""):
            pass


def test_bind_trace_id_nests_and_restores_outer_value() -> None:
    with bind_trace_id("outer"):
        assert current_trace_id() == "outer"
        with bind_trace_id("inner"):
            assert current_trace_id() == "inner"
        assert current_trace_id() == "outer"
    assert current_trace_id() == ""
