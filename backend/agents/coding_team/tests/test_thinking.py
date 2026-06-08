"""Tests for the coding-team agent-thinking capture: the reasoning buffer, the
flush-interval resolver, and the /status `thinking` surface."""

from __future__ import annotations

from typing import Any, Dict

from fastapi.testclient import TestClient

from coding_team.api import main as api
from coding_team.orchestrator import (
    _DEFAULT_THINKING_FLUSH_INTERVAL_S,
    _flush_thinking,
    _make_reasoning_llm_getter,
    _thinking_flush_interval_s,
    _ThinkingBuffer,
)

client = TestClient(api.app)


# --------------------------------------------------------------------------- buffer


def test_buffer_accumulates_and_reports_change() -> None:
    buf = _ThinkingBuffer()
    buf.append("thinking ")
    buf.append("hard")
    assert buf.snapshot_if_changed() == "thinking hard"
    # No change since the last snapshot → None (so the flusher skips a redundant write).
    assert buf.snapshot_if_changed() is None
    buf.append("er")
    assert buf.snapshot_if_changed() == "thinking harder"


def test_buffer_keeps_only_recent_tail() -> None:
    buf = _ThinkingBuffer(max_chars=5)
    buf.append("abcdef")
    buf.append("gh")
    assert buf.snapshot_if_changed() == "defgh"  # last 5 chars only


def test_buffer_empty_is_no_change() -> None:
    buf = _ThinkingBuffer()
    # Initial empty text equals the never-flushed sentinel only after a first flush;
    # before any append the snapshot is the empty string (a change from None once).
    first = buf.snapshot_if_changed()
    assert first == ""
    assert buf.snapshot_if_changed() is None


# --------------------------------------------------------------------------- flush


def test_flush_thinking_writes_only_on_change() -> None:
    buf = _ThinkingBuffer()
    calls: list[str] = []

    def update(**kw: Any) -> None:
        calls.append(kw["thinking"])

    buf.append("abc")
    _flush_thinking(buf, update)
    _flush_thinking(buf, update)  # unchanged → no second write
    buf.append("def")
    _flush_thinking(buf, update)
    assert calls == ["abc", "abcdef"]


def test_flush_thinking_skips_blank() -> None:
    """An empty/whitespace buffer (e.g. the beat_first tick before any reasoning, or a
    path that never captures reasoning) must not write an empty thinking field."""
    calls: list[str] = []

    def update(**kw: Any) -> None:
        calls.append(kw["thinking"])

    empty = _ThinkingBuffer()
    _flush_thinking(empty, update)  # buffer is "" → no write
    blank = _ThinkingBuffer()
    blank.append("   \n")
    _flush_thinking(blank, update)  # whitespace-only → no write
    assert calls == []

    # Once real content arrives it flushes.
    blank.append("real")
    _flush_thinking(blank, update)
    assert calls == ["   \nreal"]


def test_flush_thinking_swallows_update_errors() -> None:
    buf = _ThinkingBuffer()
    buf.append("x")

    def boom(**_kw: Any) -> None:
        raise RuntimeError("db down")

    # Must not raise — surfacing thinking is best-effort.
    _flush_thinking(buf, boom)


# --------------------------------------------------------------------------- getter


def test_make_reasoning_llm_getter_threads_callback(monkeypatch) -> None:
    """The default getter builds an uncached, hook-bearing client and wraps it."""
    import llm_service.factory as factory
    import llm_service.strands_provider as strands_provider

    captured: Dict[str, Any] = {}

    sentinel_client = object()
    sentinel_model = object()

    def fake_get_client(key, *, on_reasoning=None):
        captured["key"] = key
        captured["on_reasoning"] = on_reasoning
        return sentinel_client

    def fake_get_strands_model(key, *, client=None, **_kw):
        captured["model_key"] = key
        captured["client"] = client
        return sentinel_model

    monkeypatch.setattr(factory, "get_client", fake_get_client)
    monkeypatch.setattr(strands_provider, "get_strands_model", fake_get_strands_model)

    cb = lambda _t: None  # noqa: E731
    getter = _make_reasoning_llm_getter(cb)
    model = getter("tech_lead")

    assert model is sentinel_model
    assert captured["key"] == "tech_lead"
    assert captured["on_reasoning"] is cb
    assert captured["client"] is sentinel_client


# --------------------------------------------------------------------------- interval


def test_flush_interval_default(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_THINKING_FLUSH_INTERVAL_S", raising=False)
    assert _thinking_flush_interval_s() == _DEFAULT_THINKING_FLUSH_INTERVAL_S


def test_flush_interval_override(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_THINKING_FLUSH_INTERVAL_S", "0.5")
    assert _thinking_flush_interval_s() == 0.5


def test_flush_interval_garbage_and_nonpositive_fall_back(monkeypatch) -> None:
    # Includes non-finite (inf/nan): a non-finite interval would make the heartbeat's
    # Event.wait(interval) block forever, never flushing.
    for bad in ("", "abc", "0", "-3", "inf", "-inf", "nan"):
        monkeypatch.setenv("AGENT_THINKING_FLUSH_INTERVAL_S", bad)
        assert _thinking_flush_interval_s() == _DEFAULT_THINKING_FLUSH_INTERVAL_S


# --------------------------------------------------------------------------- /status


def _job(**over: Any) -> Dict[str, Any]:
    base = {
        "job_id": "j1",
        "status": "running",
        "phase": "coding",
        "task_graph_snapshot": [],
    }
    base.update(over)
    return base


def test_status_surfaces_thinking(monkeypatch) -> None:
    monkeypatch.setattr(api, "get_job", lambda jid: _job(thinking="weighing the design"))
    r = client.get("/status/j1")
    assert r.status_code == 200
    assert r.json()["thinking"] == "weighing the design"


def test_status_thinking_absent_is_null(monkeypatch) -> None:
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    r = client.get("/status/j1")
    assert r.status_code == 200
    assert r.json()["thinking"] is None
