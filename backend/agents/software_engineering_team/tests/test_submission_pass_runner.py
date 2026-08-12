"""Tests for the shared submission-pass runner (bisect recovery, no char caps)."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import code_review_agent.submission_pass_runner as runner_mod
import pytest
from code_review_agent.submission_pass_runner import (
    FileBatch,
    _call_agent,
    _is_overflow_shaped,
    run_submission_pass,
)
from strands.types.exceptions import ContextWindowOverflowException, MaxTokensReachedException

from llm_service import LLMTruncatedError
from llm_service.clients.dummy import DummyLLMClient


def _paths_prompt(batch: FileBatch) -> str:
    return "PATHS:" + ",".join(path for path, _ in batch.items)


def _patch_via_reasoning_json(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    """Stub ``run_agent_via_reasoning`` to invoke ``handler(reasoning_prompt)`` -> dict."""

    def _fake(**kwargs: Any) -> Any:
        data = handler(kwargs["reasoning_prompt"])
        if isinstance(data, BaseException):
            raise data
        return kwargs["parse"](json.dumps(data))

    monkeypatch.setattr(runner_mod, "run_agent_via_reasoning", _fake)


def test_call_agent_delegates_to_run_agent_via_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def _fake(**kwargs: Any) -> dict[str, str]:
        seen.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(runner_mod, "run_agent_via_reasoning", _fake)
    sentinel_tools = [{"name": "list_files"}]
    result = _call_agent(
        object(),
        "reasoning sys",
        "format json",
        sentinel_tools,
        "user prompt",
        json.loads,
    )
    assert result == {"ok": True}
    assert len(seen) == 1
    assert seen[0]["reasoning_system_prompt"] == "reasoning sys"
    assert seen[0]["formatting_instructions"] == "format json"
    assert seen[0]["reasoning_prompt"] == "user prompt"
    assert seen[0]["tools"] == sentinel_tools
    assert seen[0]["reasoning_think"] is True


class _FailIfAsked(DummyLLMClient):
    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        raise AssertionError(f"must not call the LLM, got prompt: {prompt!r}")


def test_is_overflow_shaped_classifies_known_and_unknown_exceptions() -> None:
    assert _is_overflow_shaped(ContextWindowOverflowException("x")) is True
    assert _is_overflow_shaped(MaxTokensReachedException("x")) is True
    assert (
        _is_overflow_shaped(LLMTruncatedError("x", partial_content="", finish_reason="length"))
        is True
    )
    assert _is_overflow_shaped(RuntimeError("x")) is False
    assert _is_overflow_shaped(json.JSONDecodeError("bad", "doc", 0)) is False


def test_is_overflow_shaped_matches_provider_prompt_too_large_messages() -> None:
    """Generic 4xx wrappers that name context/prompt length must recover."""
    from llm_service.interface import LLMPermanentError

    assert _is_overflow_shaped(LLMPermanentError("prompt is too long for the model")) is True
    assert _is_overflow_shaped(RuntimeError("Request exceeds the context window")) is True
    wrapped = LLMPermanentError("bad request")
    wrapped.__cause__ = ValueError("input too long: 200000 tokens")
    assert _is_overflow_shaped(wrapped) is True
    assert _is_overflow_shaped(LLMPermanentError("invalid api key")) is False


def test_returns_empty_and_makes_no_call_for_empty_changed_files() -> None:
    result = run_submission_pass(
        _FailIfAsked(),
        changed_files=[],
        reasoning_system_prompt="sys",
        formatting_instructions="fmt",
        build_prompt=_paths_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == []


def test_single_call_inlines_full_changed_file_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_batches: List[FileBatch] = []

    def build_prompt(batch: FileBatch) -> str:
        seen_batches.append(batch)
        return _paths_prompt(batch)

    def _handler(prompt: str) -> Any:
        return {"paths": prompt.replace("PATHS:", "")}

    _patch_via_reasoning_json(monkeypatch, _handler)

    files = [("a.py", "aaaa"), ("b.py", "bbbb"), ("c.py", "cccc")]
    result = run_submission_pass(
        DummyLLMClient(),
        changed_files=files,
        reasoning_system_prompt="sys",
        formatting_instructions="fmt",
        build_prompt=build_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == [{"paths": "a.py,b.py,c.py"}]
    assert len(seen_batches) == 1
    assert seen_batches[0].items == files
    assert seen_batches[0].is_partial is False


def test_build_prompt_receives_full_file_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_content: List[str] = []

    def build_prompt(batch: FileBatch) -> str:
        for _path, content in batch.items:
            seen_content.append(content)
        return "ok"

    _patch_via_reasoning_json(monkeypatch, lambda _p: {"ok": True})

    big = "X" * 50_000
    run_submission_pass(
        DummyLLMClient(),
        changed_files=[("big.py", big)],
        reasoning_system_prompt="sys",
        formatting_instructions="fmt",
        build_prompt=build_prompt,
        tools=[],
        parse=json.loads,
    )
    assert seen_content == [big]


def test_reactive_bisect_recovers_multi_file_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_batches: List[FileBatch] = []

    def build_prompt(batch: FileBatch) -> str:
        seen_batches.append(batch)
        return _paths_prompt(batch)

    def _handler(prompt: str) -> Any:
        paths = prompt.replace("PATHS:", "")
        if "," in paths:
            raise MaxTokensReachedException("too large")
        return {"file": paths}

    _patch_via_reasoning_json(monkeypatch, _handler)

    files = [("a.py", "aaaa"), ("b.py", "bbbb")]
    result = run_submission_pass(
        DummyLLMClient(),
        changed_files=files,
        reasoning_system_prompt="sys",
        formatting_instructions="fmt",
        build_prompt=build_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == [{"file": "a.py"}, {"file": "b.py"}]
    assert [b.is_partial for b in seen_batches] == [False, True, True]


def test_provider_prompt_too_large_message_triggers_bisect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from llm_service.interface import LLMPermanentError

    seen_batches: List[FileBatch] = []

    def build_prompt(batch: FileBatch) -> str:
        seen_batches.append(batch)
        return _paths_prompt(batch)

    def _handler(prompt: str) -> Any:
        paths = prompt.replace("PATHS:", "")
        if "," in paths:
            raise LLMPermanentError("prompt is too long for the model context")
        return {"file": paths}

    _patch_via_reasoning_json(monkeypatch, _handler)

    files = [("a.py", "aaaa"), ("b.py", "bbbb")]
    result = run_submission_pass(
        DummyLLMClient(),
        changed_files=files,
        reasoning_system_prompt="sys",
        formatting_instructions="fmt",
        build_prompt=build_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == [{"file": "a.py"}, {"file": "b.py"}]
    assert [b.is_partial for b in seen_batches] == [False, True, True]


def test_single_file_overflow_skips_without_truncating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[str] = []

    def _handler(prompt: str) -> Any:
        calls.append(prompt)
        raise MaxTokensReachedException("always too large")

    _patch_via_reasoning_json(monkeypatch, _handler)

    files = [("only.py", "X" * 200)]
    result = run_submission_pass(
        DummyLLMClient(),
        changed_files=files,
        reasoning_system_prompt="sys",
        formatting_instructions="fmt",
        build_prompt=lambda batch: "LEN:" + str(len(batch.items[0][1])),
        tools=[],
        parse=json.loads,
    )
    assert result == []
    # No content shrink — one attempt with full length, then skip.
    assert calls == ["LEN:200"]


def test_non_overflow_failure_skips_without_bisect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def _handler(_prompt: str) -> Any:
        nonlocal calls
        calls += 1
        raise RuntimeError("malformed json")

    _patch_via_reasoning_json(monkeypatch, _handler)

    result = run_submission_pass(
        DummyLLMClient(),
        changed_files=[("a.py", "a"), ("b.py", "b")],
        reasoning_system_prompt="sys",
        formatting_instructions="fmt",
        build_prompt=_paths_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == []
    assert calls == 1


def test_context_window_overflow_bisects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(prompt: str) -> Any:
        paths = prompt.replace("PATHS:", "")
        if "," in paths:
            raise ContextWindowOverflowException("context")
        return {"file": paths}

    _patch_via_reasoning_json(monkeypatch, _handler)

    result = run_submission_pass(
        DummyLLMClient(),
        changed_files=[("a.py", "a"), ("b.py", "b")],
        reasoning_system_prompt="sys",
        formatting_instructions="fmt",
        build_prompt=_paths_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == [{"file": "a.py"}, {"file": "b.py"}]
