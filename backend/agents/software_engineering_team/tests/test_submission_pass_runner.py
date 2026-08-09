"""Tests for the shared submission-pass runner (budgeting/chunking/recovery)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import code_review_agent.submission_pass_runner as runner_mod
import pytest
from code_review_agent.submission_pass_runner import (
    FileBatch,
    _estimated_file_block_chars,
    _is_overflow_shaped,
    _manifest_chars,
    _pack_batches,
    _shrink_items,
    _with_output_budget,
    run_submission_pass,
)
from strands.types.exceptions import ContextWindowOverflowException, MaxTokensReachedException

from llm_service import LLMClientModel, LLMTruncatedError
from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.shared.context_sizing import MergedPassBudgets


def _fixed_budgets(
    *,
    max_inline_code_chars: int,
    max_manifest_chars: int = 10_000,
    max_architecture_chars: int = 0,
) -> MergedPassBudgets:
    return MergedPassBudgets(
        max_architecture_chars=max_architecture_chars,
        max_inline_code_chars=max_inline_code_chars,
        max_manifest_chars=max_manifest_chars,
        reserved_response_tokens=4096,
    )


def _paths_prompt(batch: FileBatch, _budgets: Any) -> str:
    return "PATHS:" + ",".join(path for path, _ in batch.items)


class _FailIfAsked(DummyLLMClient):
    def get_max_context_tokens(self) -> int:
        raise AssertionError("must not compute budgets when no call should be made")

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        raise AssertionError(f"must not call the LLM, got prompt: {prompt!r}")


# --- Pure helper unit tests -------------------------------------------------


def test_manifest_chars_counts_header_and_paths() -> None:
    assert _manifest_chars([]) == len("**Changed files in this submission (0):**\n")
    n = _manifest_chars(["a.py", "bb.py"])
    assert n == len("**Changed files in this submission (2):**\n") + len("a.py\n") + len("bb.py\n")


def test_estimated_file_block_chars_at_least_content_length() -> None:
    assert _estimated_file_block_chars("a.py", "hello") >= len("hello")


def test_pack_batches_empty_items() -> None:
    assert _pack_batches([], max_chars=1_000) == []


def test_pack_batches_single_batch_when_under_budget() -> None:
    items = [("a.py", "x" * 50), ("b.py", "y" * 50)]
    assert _pack_batches(items, max_chars=10_000) == [items]


def test_pack_batches_single_batch_when_max_chars_non_positive() -> None:
    items = [("a.py", "x" * 50), ("b.py", "y" * 50)]
    assert _pack_batches(items, max_chars=0) == [items]
    assert _pack_batches(items, max_chars=-5) == [items]


def test_pack_batches_splits_when_over_budget() -> None:
    items = [("a.py", "x" * 100), ("b.py", "y" * 100), ("c.py", "z" * 100)]
    per_file = _estimated_file_block_chars(*items[0])
    batches = _pack_batches(items, max_chars=per_file + 10)
    assert batches == [[items[0]], [items[1]], [items[2]]]


def test_pack_batches_keeps_oversized_file_alone() -> None:
    items = [("small.py", "a" * 10), ("huge.py", "b" * 10_000)]
    batches = _pack_batches(items, max_chars=500)
    assert batches[0] == [items[0]]
    assert batches[-1] == [items[1]]
    assert sum(len(b) for b in batches) == len(items)


def test_shrink_items_halves_content() -> None:
    shrunk = _shrink_items([("a.py", "a" * 10), ("b.py", "b" * 4)])
    assert shrunk == [("a.py", "a" * 5), ("b.py", "b" * 2)]


def test_shrink_items_returns_none_when_nothing_left_to_shrink() -> None:
    assert _shrink_items([("a.py", ""), ("b.py", "")]) is None


def test_with_output_budget_leaves_non_llm_client_model_unchanged() -> None:
    client = DummyLLMClient()
    assert _with_output_budget(client, response_tokens=4096) is client


def test_with_output_budget_clones_when_effective_cap_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)
    model = LLMClientModel(DummyLLMClient(), max_tokens=None)
    result = _with_output_budget(model, response_tokens=4096)
    assert result is not model
    assert result.get_config().get("max_tokens") == 4096


def test_with_output_budget_returns_same_model_when_cap_already_matches() -> None:
    model = LLMClientModel(DummyLLMClient(), max_tokens=4096)
    result = _with_output_budget(model, response_tokens=4096)
    assert result is model


def test_is_overflow_shaped_classifies_known_and_unknown_exceptions() -> None:
    assert _is_overflow_shaped(ContextWindowOverflowException("x")) is True
    assert _is_overflow_shaped(MaxTokensReachedException("x")) is True
    assert (
        _is_overflow_shaped(LLMTruncatedError("x", partial_content="", finish_reason="length"))
        is True
    )
    assert _is_overflow_shaped(RuntimeError("x")) is False
    assert _is_overflow_shaped(json.JSONDecodeError("bad", "doc", 0)) is False


# --- run_submission_pass: budgeting / no-op paths ---------------------------


def test_returns_empty_and_makes_no_call_for_empty_changed_files() -> None:
    result = run_submission_pass(
        _FailIfAsked(),
        changed_files=[],
        system_prompt="sys",
        build_prompt=_paths_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == []


def test_returns_empty_when_budgets_computation_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_mod, "compute_code_review_merged_pass_budgets", lambda *a, **k: None)
    result = run_submission_pass(
        _FailIfAsked(),
        changed_files=[("a.py", "code")],
        system_prompt="sys",
        build_prompt=_paths_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == []


def test_returns_empty_when_context_too_small_for_fixed_prompt() -> None:
    """No monkeypatch: exercises the real budgeting function end to end."""

    class _FailIfCalled(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            raise AssertionError(f"must not call the LLM, got prompt: {prompt!r}")

    huge_system_prompt = "x" * 500_000  # far exceeds DummyLLMClient's fixed context window
    result = run_submission_pass(
        _FailIfCalled(),
        changed_files=[("a.py", "code")],
        system_prompt=huge_system_prompt,
        build_prompt=_paths_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == []


# --- run_submission_pass: proactive chunking --------------------------------


def test_single_batch_and_single_call_when_everything_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: _fixed_budgets(max_inline_code_chars=100_000),
    )
    seen_batches: List[FileBatch] = []

    def build_prompt(batch: FileBatch, _budgets: Any) -> str:
        seen_batches.append(batch)
        return _paths_prompt(batch, _budgets)

    calls: List[str] = []

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            calls.append(prompt)
            return {"paths": prompt.replace("PATHS:", "")}

    files = [("a.py", "x" * 10), ("b.py", "y" * 10)]
    result = run_submission_pass(
        _Client(),
        changed_files=files,
        system_prompt="sys",
        build_prompt=build_prompt,
        tools=[],
        parse=json.loads,
        pass_label="TestPass",
    )

    assert len(calls) == 1
    assert len(seen_batches) == 1
    assert seen_batches[0] == FileBatch(items=files, index=1, total=1)
    assert result == [{"paths": "a.py,b.py"}]


def test_splits_into_multiple_batches_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [("a.py", "x" * 100), ("b.py", "y" * 100), ("c.py", "z" * 100)]
    per_file = _estimated_file_block_chars(*items[0])
    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: _fixed_budgets(max_inline_code_chars=per_file + 10),
    )
    seen_batches: List[FileBatch] = []

    def build_prompt(batch: FileBatch, _budgets: Any) -> str:
        seen_batches.append(batch)
        return _paths_prompt(batch, _budgets)

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"paths": prompt.replace("PATHS:", "")}

    result = run_submission_pass(
        _Client(),
        changed_files=items,
        system_prompt="sys",
        build_prompt=build_prompt,
        tools=[],
        parse=json.loads,
    )

    assert len(seen_batches) == 3
    assert [b.index for b in seen_batches] == [1, 2, 3]
    assert all(b.total == 3 for b in seen_batches)
    assert [b.items for b in seen_batches] == [[items[0]], [items[1]], [items[2]]]
    assert result == [{"paths": "a.py"}, {"paths": "b.py"}, {"paths": "c.py"}]


def test_max_extra_body_chars_propagates_from_computed_architecture_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner must expose the computed extra-body allowance to
    build_prompt, not discard it — otherwise a pass that inlines a
    pass-specific body (e.g. an architecture document) has no way to know
    how much of it actually fits."""
    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: _fixed_budgets(max_inline_code_chars=100_000, max_architecture_chars=777),
    )
    seen_budgets = []

    def build_prompt(batch: FileBatch, budgets: Any) -> str:
        seen_budgets.append(budgets)
        return _paths_prompt(batch, budgets)

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"paths": prompt.replace("PATHS:", "")}

    run_submission_pass(
        _Client(),
        changed_files=[("a.py", "x")],
        system_prompt="sys",
        build_prompt=build_prompt,
        tools=[],
        parse=json.loads,
        extra_reserved_chars=1_000,
    )
    assert len(seen_budgets) == 1
    assert seen_budgets[0].max_extra_body_chars == 777


# --- run_submission_pass: reactive recovery ---------------------------------


def test_reactive_bisect_recovers_when_multi_file_batch_overflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both files fit in one proactive batch, but the combined call overflows;
    the runner must bisect into two single-file calls that each succeed."""
    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: _fixed_budgets(max_inline_code_chars=100_000),
    )

    seen_batches: List[FileBatch] = []

    def build_prompt(batch: FileBatch, _budgets: Any) -> str:
        seen_batches.append(batch)
        return _paths_prompt(batch, _budgets)

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            paths = prompt.replace("PATHS:", "")
            if "," in paths:
                raise ContextWindowOverflowException("combined batch too large")
            return {"file": paths}

    files = [("a.py", "aaaa"), ("b.py", "bbbb")]
    result = run_submission_pass(
        _Client(),
        changed_files=files,
        system_prompt="sys",
        build_prompt=build_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == [{"file": "a.py"}, {"file": "b.py"}]
    # The original combined attempt is not partial; the two bisected retries are.
    assert [b.is_partial for b in seen_batches] == [False, True, True]
    assert all(b.total == 1 and b.index == 1 for b in seen_batches), (
        "bisected children keep the parent's index/total, but is_partial tells "
        "build_prompt they no longer represent the full batch"
    )


def test_reactive_shrink_recovers_when_single_file_batch_overflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: _fixed_budgets(max_inline_code_chars=100_000),
    )

    seen_batches: List[FileBatch] = []

    def build_prompt(batch: FileBatch, _budgets: Any) -> str:
        seen_batches.append(batch)
        return "LEN:" + str(len(batch.items[0][1]))

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            length = int(prompt.replace("LEN:", ""))
            if length >= 150:
                raise MaxTokensReachedException("output too long for this much input")
            return {"ok": True, "len": length}

    files = [("only.py", "X" * 200)]
    result = run_submission_pass(
        _Client(),
        changed_files=files,
        system_prompt="sys",
        build_prompt=build_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == [{"ok": True, "len": 100}]
    # The original attempt is not partial; the shrunk retry is (its content is
    # not the full file body).
    assert [b.is_partial for b in seen_batches] == [False, True]


def test_reactive_shrink_gives_up_after_one_failed_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: _fixed_budgets(max_inline_code_chars=100_000),
    )
    calls: List[str] = []

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            calls.append(prompt)
            raise MaxTokensReachedException("always too large")

    files = [("only.py", "X" * 200)]
    result = run_submission_pass(
        _Client(),
        changed_files=files,
        system_prompt="sys",
        build_prompt=lambda batch, _b: "LEN:" + str(len(batch.items[0][1])),
        tools=[],
        parse=json.loads,
    )
    assert result == []
    # Exactly one original attempt plus one shrink-and-retry attempt; no further retries.
    assert calls == ["LEN:200", "LEN:100"]


def test_overflow_on_unshrinkable_single_file_batch_skips_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-content single-file batch cannot be bisected (one file) or
    shrunk further (nothing left to halve); it must be skipped after the
    first failed attempt, with no shrink retry."""
    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: _fixed_budgets(max_inline_code_chars=100_000),
    )
    calls: List[str] = []

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            calls.append(prompt)
            raise ContextWindowOverflowException("overflow even with no content")

    files = [("only.py", "")]
    result = run_submission_pass(
        _Client(),
        changed_files=files,
        system_prompt="sys",
        build_prompt=_paths_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == []
    assert len(calls) == 1


def test_non_overflow_exception_skips_batch_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [("a.py", "x" * 50), ("b.py", "y" * 50)]
    per_file = _estimated_file_block_chars(*items[0])
    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: _fixed_budgets(max_inline_code_chars=per_file + 5),
    )
    calls: List[str] = []

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            calls.append(prompt)
            if "a.py" in prompt:
                raise RuntimeError("malformed reply, not overflow-shaped")
            return {"paths": prompt.replace("PATHS:", "")}

    result = run_submission_pass(
        _Client(),
        changed_files=items,
        system_prompt="sys",
        build_prompt=_paths_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == [{"paths": "b.py"}]
    # One call per batch; the failing batch is never retried.
    assert len(calls) == 2


def test_depth_cap_bounds_bisection_and_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An always-overflowing 32-file batch must stop bisecting at the depth
    cap (never reaching single-file granularity) rather than recursing
    without bound."""
    files: List[Tuple[str, str]] = [(f"f{i}.py", "x" * 10) for i in range(32)]
    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: _fixed_budgets(max_inline_code_chars=100_000),
    )
    seen_sizes: List[int] = []

    def build_prompt(batch: FileBatch, _budgets: Any) -> str:
        seen_sizes.append(len(batch.items))
        return "SIZE:" + str(len(batch.items))

    call_count = 0

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            raise MaxTokensReachedException("always too large")

    result = run_submission_pass(
        _Client(),
        changed_files=files,
        system_prompt="sys",
        build_prompt=build_prompt,
        tools=[],
        parse=json.loads,
    )
    assert result == []
    assert 1 not in seen_sizes, "depth cap must stop bisection before single-file granularity"
    assert min(seen_sizes) == 2
    assert call_count < 200  # bounded, not unbounded/exponential-without-limit
