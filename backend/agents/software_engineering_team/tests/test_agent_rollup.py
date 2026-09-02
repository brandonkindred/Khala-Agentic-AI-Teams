"""Unit tests for the agent/phase rollup result shape (metrics.agent_rollup).

This step defines the dataclass shape only — no computation exists yet, so these
tests cover construction defaults and (de)serialization mechanics, not any
ratio/percentile values.
"""

from __future__ import annotations

import json

from software_engineering_team.metrics.agent_rollup import AgentRollupMetrics, CallRollup


def test_call_rollup_defaults() -> None:
    """A default CallRollup represents an empty group: zeros and None derived stats."""
    r = CallRollup()
    assert r.call_count == 0
    assert r.total_cost_usd == 0.0
    assert r.total_input_tokens == 0
    assert r.total_output_tokens == 0
    assert r.total_cache_read_tokens == 0
    assert r.total_cache_creation_tokens == 0
    assert r.cache_read_ratio is None
    assert r.latency_ms_median is None
    assert r.latency_ms_p95 is None
    assert r.latency_ms_sample_count == 0


def test_agent_rollup_metrics_defaults() -> None:
    """A default AgentRollupMetrics carries no groups in any of the three views."""
    m = AgentRollupMetrics(window_days=7.0, computed_at="2026-09-02T00:00:00+00:00")
    assert m.window_days == 7.0
    assert m.computed_at == "2026-09-02T00:00:00+00:00"
    assert m.by_agent == {}
    assert m.by_phase == {}
    assert m.by_agent_phase == {}


def test_agent_rollup_metrics_mutable_defaults_are_isolated() -> None:
    """Two independent instances don't share the same default dict (the field(default_factory) footgun)."""
    a = AgentRollupMetrics(window_days=1.0, computed_at="t")
    b = AgentRollupMetrics(window_days=1.0, computed_at="t")

    a.by_agent["backend"] = CallRollup(call_count=1)
    a.by_phase["execution"] = CallRollup(call_count=1)
    a.by_agent_phase["backend"] = {"execution": CallRollup(call_count=1)}

    assert b.by_agent == {}
    assert b.by_phase == {}
    assert b.by_agent_phase == {}


def test_to_dict_round_trip_nests_plain_dicts() -> None:
    """to_dict() recurses through every level, including the by_agent_phase nesting."""
    m = AgentRollupMetrics(window_days=30.0, computed_at="2026-09-02T00:00:00+00:00")
    m.by_agent["backend"] = CallRollup(
        call_count=3,
        total_cost_usd=1.23,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cache_read_tokens=40,
        total_cache_creation_tokens=10,
        cache_read_ratio=0.4,
        latency_ms_median=250.0,
        latency_ms_p95=900.0,
        latency_ms_sample_count=3,
    )
    m.by_phase["execution"] = CallRollup(call_count=3, total_cost_usd=1.23)
    m.by_agent_phase["backend"] = {"execution": CallRollup(call_count=3, total_cost_usd=1.23)}

    d = m.to_dict()

    # The documented invariant: the whole shape serializes to JSON end to end.
    assert json.loads(json.dumps(d)) == d

    assert d["window_days"] == 30.0
    assert d["computed_at"] == "2026-09-02T00:00:00+00:00"

    # Every nested value is a plain dict, not a CallRollup instance.
    assert isinstance(d["by_agent"]["backend"], dict)
    assert isinstance(d["by_phase"]["execution"], dict)
    assert isinstance(d["by_agent_phase"]["backend"], dict)
    assert isinstance(d["by_agent_phase"]["backend"]["execution"], dict)

    assert d["by_agent"]["backend"]["call_count"] == 3
    assert d["by_agent"]["backend"]["cache_read_ratio"] == 0.4
    assert d["by_agent"]["backend"]["latency_ms_median"] == 250.0
    assert d["by_agent"]["backend"]["latency_ms_p95"] == 900.0
    assert d["by_phase"]["execution"]["total_cost_usd"] == 1.23
    assert d["by_agent_phase"]["backend"]["execution"]["call_count"] == 3

    # A group with no samples keeps its None sentinels through serialization.
    assert d["by_phase"]["execution"]["cache_read_ratio"] is None
    assert d["by_phase"]["execution"]["latency_ms_median"] is None
