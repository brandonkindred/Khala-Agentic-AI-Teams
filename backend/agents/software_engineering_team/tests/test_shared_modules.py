"""Tests for the SE team's shared utility modules.

Covers smaller 0%-coverage files: planning_consolidation, task_validation,
tool_recommendation_guidance, post_mortem (already tested via test_recovery_flow
but pytest doesn't collect those), continuation, decomposition.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# planning_consolidation
# ---------------------------------------------------------------------------


def test_planning_consolidation_writes_master_plan(tmp_path: Path) -> None:
    from software_engineering_team.shared.planning_consolidation import (
        run_planning_consolidation,
    )

    # Create some artifact files to exercise the existence check
    (tmp_path / "architecture.md").write_text("# Arch")
    (tmp_path / "data_schema.md").write_text("# Schema")

    assignment = SimpleNamespace(execution_order=["t1", "t2"])
    architecture = SimpleNamespace()
    overview = {
        "risk_items": [
            {"description": "risk1", "severity": "high", "mitigation": "do x"},
            {"description": "risk2", "severity": "medium", "mitigation": ""},
        ],
    }

    out = run_planning_consolidation(tmp_path, assignment, architecture, overview)
    assert out.exists()
    content = out.read_text()
    assert "# Master Plan" in content
    assert "## Execution Order" in content
    assert "t1, t2" in content
    assert "## Risk Register" in content
    assert "risk1" in content
    assert "Mitigation: do x" in content
    assert "## Ship Checklist" in content
    assert "[architecture.md]" in content


def test_planning_consolidation_without_assignment(tmp_path: Path) -> None:
    from software_engineering_team.shared.planning_consolidation import (
        run_planning_consolidation,
    )

    out = run_planning_consolidation(tmp_path, None, None, None)
    assert out.exists()
    content = out.read_text()
    assert "## Execution Order" not in content
    assert "## Risk Register" not in content


def test_planning_consolidation_backend_openapi(tmp_path: Path) -> None:
    from software_engineering_team.shared.planning_consolidation import (
        run_planning_consolidation,
    )

    # Mock backend/openapi.yaml in parent
    backend_dir = tmp_path.parent / "backend"
    backend_dir.mkdir(parents=True, exist_ok=True)
    (backend_dir / "openapi.yaml").write_text("openapi: 3.0.0")

    try:
        out = run_planning_consolidation(tmp_path, None, None, None)
        content = out.read_text()
        assert "backend/openapi.yaml" in content
    finally:
        (backend_dir / "openapi.yaml").unlink(missing_ok=True)
        backend_dir.rmdir()


# ---------------------------------------------------------------------------
# task_validation
# ---------------------------------------------------------------------------


def _make_task(id: str, deps=None):
    from shared.dev_models.models import Task, TaskType

    return Task(
        id=id,
        type=TaskType.BACKEND,
        assignee="backend",
        description=f"task {id}",
        dependencies=deps or [],
    )


def test_validate_task_empty_id() -> None:
    from software_engineering_team.shared.task_validation import validate_task

    t = _make_task("")
    errors = validate_task(t, {"a", "b"})
    assert any("empty id" in e for e in errors)


def test_validate_task_missing_dependency() -> None:
    from software_engineering_team.shared.task_validation import validate_task

    t = _make_task("x", ["missing"])
    errors = validate_task(t, {"x"})
    assert any("non-existent" in e for e in errors)


def test_validate_assignment_happy_path() -> None:
    from shared.dev_models.models import TaskAssignment
    from software_engineering_team.shared.task_validation import validate_assignment

    tasks = [_make_task("a"), _make_task("b", ["a"])]
    assignment = TaskAssignment(tasks=tasks, execution_order=["a", "b"])
    ok, errors = validate_assignment(assignment)
    assert ok is True
    assert errors == []


def test_validate_assignment_order_references_missing_task() -> None:
    from shared.dev_models.models import TaskAssignment
    from software_engineering_team.shared.task_validation import validate_assignment

    tasks = [_make_task("a")]
    assignment = TaskAssignment(tasks=tasks, execution_order=["a", "ghost"])
    ok, errors = validate_assignment(assignment)
    assert ok is False
    assert any("ghost" in e for e in errors)


def test_validate_assignment_task_missing_from_execution_order() -> None:
    from shared.dev_models.models import TaskAssignment
    from software_engineering_team.shared.task_validation import validate_assignment

    tasks = [_make_task("a"), _make_task("b")]
    assignment = TaskAssignment(tasks=tasks, execution_order=["a"])
    ok, errors = validate_assignment(assignment)
    assert ok is False
    assert any("b" in e and "execution_order" in e for e in errors)


# ---------------------------------------------------------------------------
# tool_recommendation_guidance
# ---------------------------------------------------------------------------


def test_tool_recommendation_guidance_cached() -> None:
    from software_engineering_team.shared import tool_recommendation_guidance as trg

    # Reset cache
    trg._tool_recommendation_guidance_cache = None
    g1 = trg.get_tool_recommendation_guidance_cached()
    g2 = trg.get_tool_recommendation_guidance_cached()
    assert g1 is g2
    assert "TOOL RECOMMENDATION STANDARDS" in g1


# ---------------------------------------------------------------------------
# post_mortem
# ---------------------------------------------------------------------------


def test_post_mortem_writer_writes_failure(tmp_path: Path) -> None:
    from software_engineering_team.shared.post_mortem import PostMortemWriter

    writer = PostMortemWriter(project_root=tmp_path)
    path = writer.write_failure(
        agent_name="TestAgent",
        task_description="Test task",
        original_prompt="prompt",
        partial_responses=["p1", "p2"],
        continuation_attempts=5,
        decomposition_depth=3,
        error=RuntimeError("boom"),
    )
    assert path.exists()
    content = path.read_text()
    assert "TestAgent" in content
    assert "5/5" in content
    assert "3/20" in content
    assert "boom" in content
    assert "Post-Mortem Log" in content  # header written for new file


def test_post_mortem_writer_appends(tmp_path: Path) -> None:
    from software_engineering_team.shared.post_mortem import PostMortemWriter

    writer = PostMortemWriter(project_root=tmp_path)
    writer.write_failure(
        agent_name="A",
        task_description="t1",
        original_prompt="p",
        partial_responses=[],
        continuation_attempts=1,
        decomposition_depth=1,
        error=RuntimeError("e1"),
    )
    writer.write_failure(
        agent_name="B",
        task_description="t2",
        original_prompt="p",
        partial_responses=[],
        continuation_attempts=1,
        decomposition_depth=1,
        error=RuntimeError("e2"),
    )
    content = writer.post_mortem_file.read_text()
    assert content.count("Post-Mortem Log") == 1  # header written once
    assert "A" in content and "B" in content


def test_post_mortem_writer_default_cwd(tmp_path, monkeypatch) -> None:
    from software_engineering_team.shared.post_mortem import PostMortemWriter

    monkeypatch.chdir(tmp_path)
    writer = PostMortemWriter()
    assert writer.project_root == tmp_path


def test_post_mortem_truncate_text(tmp_path: Path) -> None:
    from software_engineering_team.shared.post_mortem import PostMortemWriter

    writer = PostMortemWriter(project_root=tmp_path)
    short = writer._truncate_text("hi", 10)
    assert short == "hi"
    long_text = writer._truncate_text("x" * 100, 10)
    assert long_text.endswith("...")
    assert len(long_text) == 13


def test_post_mortem_format_partial_responses_empty(tmp_path: Path) -> None:
    from software_engineering_team.shared.post_mortem import PostMortemWriter

    writer = PostMortemWriter(project_root=tmp_path)
    out = writer._format_partial_responses([])
    assert "No partial responses" in out


def test_post_mortem_format_partial_responses_truncated(tmp_path: Path) -> None:
    from software_engineering_team.shared.post_mortem import PostMortemWriter

    writer = PostMortemWriter(project_root=tmp_path)
    out = writer._format_partial_responses(
        ["a" * 300, "b" * 300, "c", "d"], max_responses=2, max_per_response=50
    )
    assert "and 2 more" in out


def test_post_mortem_suggestions_all_paths(tmp_path: Path) -> None:
    from software_engineering_team.shared.post_mortem import PostMortemWriter

    writer = PostMortemWriter(project_root=tmp_path)
    suggestions = writer._generate_suggestions(
        continuation_attempts=5,
        decomposition_depth=20,
        partial_responses=["x" * 60000],
        max_continuation_cycles=5,
        max_decomposition_depth=20,
    )
    assert "Increase continuation cycles" in suggestions
    assert "Simplify the task" in suggestions
    assert "Reduce output size" in suggestions
    assert "Review token limits" in suggestions


def test_write_post_mortem_convenience(tmp_path: Path) -> None:
    from software_engineering_team.shared.post_mortem import write_post_mortem

    path = write_post_mortem(
        agent_name="A",
        task_description="t",
        original_prompt="p",
        partial_responses=["x"],
        continuation_attempts=1,
        decomposition_depth=1,
        error=Exception("err"),
        project_root=tmp_path,
    )
    assert path.exists()


# ---------------------------------------------------------------------------
# continuation
# ---------------------------------------------------------------------------


def test_continuation_ollama_auth_headers_empty(monkeypatch) -> None:
    from software_engineering_team.shared import continuation

    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LLM_OLLAMA_API_KEY", raising=False)
    assert continuation._ollama_auth_headers() == {}


def test_continuation_ollama_auth_headers_set(monkeypatch) -> None:
    from software_engineering_team.shared import continuation

    monkeypatch.setenv("OLLAMA_API_KEY", "key-placeholder")
    headers = continuation._ollama_auth_headers()
    assert headers["Authorization"].startswith("Bearer")


def test_continuation_result_dataclass() -> None:
    from software_engineering_team.shared.continuation import ContinuationResult

    r = ContinuationResult(success=True, content="x", cycles_used=1)
    assert r.success is True
    assert r.partial_responses == []


def test_continuation_exhausted_error_carries_partial() -> None:
    from software_engineering_team.shared.continuation import (
        LLMContinuationExhaustedError,
    )

    err = LLMContinuationExhaustedError(
        "exhausted", partial_content="abc", cycles_attempted=3, partial_responses=["a", "b"]
    )
    assert "exhausted" in str(err)
    assert err.partial_content == "abc"
    assert err.cycles_attempted == 3
    assert err.partial_responses == ["a", "b"]


def test_continuation_create_continuation_prompt() -> None:
    from software_engineering_team.shared.continuation import ResponseContinuator

    c = ResponseContinuator(base_url="http://x", model="m")
    prompt = c._create_continuation_prompt("abcdef" * 50)
    assert "continue exactly" in prompt.lower()


def test_continuation_build_messages_with_system() -> None:
    from software_engineering_team.shared.continuation import ResponseContinuator

    c = ResponseContinuator(base_url="http://x", model="m")
    msgs = c._build_continuation_messages(
        original_prompt="hi", partial_responses=["pa"], system_prompt="sys"
    )
    roles = [m["role"] for m in msgs]
    assert roles[0] == "system"
    assert roles == ["system", "user", "assistant", "user"]


def test_continuation_build_messages_multi_partials_no_system() -> None:
    from software_engineering_team.shared.continuation import ResponseContinuator

    c = ResponseContinuator(base_url="http://x", model="m")
    msgs = c._build_continuation_messages(original_prompt="hi", partial_responses=["p1", "p2"])
    roles = [m["role"] for m in msgs]
    # user + (assistant+user) * 2
    assert roles == ["user", "assistant", "user", "assistant", "user"]


def test_continuation_find_overlap() -> None:
    from software_engineering_team.shared.continuation import ResponseContinuator

    c = ResponseContinuator(base_url="http://x", model="m")
    assert c._find_overlap("", "abc") == 0
    assert c._find_overlap("abc", "") == 0
    # min_overlap is 10 by default — provide a long matching tail
    overlap_word = "overlapping"  # 11 chars > min 10
    assert c._find_overlap(f"prefix {overlap_word}", f"{overlap_word} suffix") == len(overlap_word)
    assert c._find_overlap("abc", "xyz") == 0


def test_continuation_merge_responses() -> None:
    from software_engineering_team.shared.continuation import ResponseContinuator

    c = ResponseContinuator(base_url="http://x", model="m")
    assert c._merge_responses([]) == ""
    assert c._merge_responses(["only"]) == "only"
    # Without overlap >= 10, no overlap-removal happens
    merged = c._merge_responses(["foo bar", "bar baz"])
    assert merged == "foo barbar baz"

    # With sufficient overlap, the duplicate boundary is removed
    overlap = "overlapping"  # >10 chars
    merged2 = c._merge_responses([f"start {overlap}", f"{overlap} end"])
    assert merged2 == f"start {overlap} end"


def test_continuation_num_predict_from_env(monkeypatch) -> None:
    from software_engineering_team.shared.continuation import ResponseContinuator

    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "1024")
    c = ResponseContinuator(base_url="http://x", model="m")
    assert c.num_predict == 1024


def test_continuation_log_response_writes_file(tmp_path) -> None:
    from software_engineering_team.shared.continuation import ResponseContinuator

    c = ResponseContinuator(base_url="http://x", model="m")
    c._log_continuation_response(
        task_id="task/1",
        cycle=1,
        response_content="hello",
        done_reason="stop",
        project_root=tmp_path,
    )
    files = list((tmp_path / "continuation_logs").glob("*.txt"))
    assert files
    content = files[0].read_text()
    assert "hello" in content


def test_continuation_attempt_success(monkeypatch) -> None:
    from software_engineering_team.shared.continuation import ResponseContinuator

    c = ResponseContinuator(base_url="http://x", model="m", max_cycles=3)

    def fake_send(messages, json_mode=False):
        return ("done!", "stop")

    monkeypatch.setattr(c, "_send_chat_request", fake_send)
    res = c.attempt_continuation(original_prompt="orig", partial_content="partial ")
    assert res.success is True
    assert "partial" in res.content


def test_continuation_attempt_exhausted_via_max_cycles(monkeypatch) -> None:
    from software_engineering_team.shared.continuation import ResponseContinuator

    c = ResponseContinuator(base_url="http://x", model="m", max_cycles=2)

    def fake_send(messages, json_mode=False):
        return ("more", "length")

    monkeypatch.setattr(c, "_send_chat_request", fake_send)
    res = c.attempt_continuation(original_prompt="orig", partial_content="partial")
    assert res.success is False
    assert res.cycles_used == 2


def test_continuation_attempt_error_in_send(monkeypatch) -> None:
    from software_engineering_team.shared.continuation import ResponseContinuator

    c = ResponseContinuator(base_url="http://x", model="m", max_cycles=3)

    def fake_send(messages, json_mode=False):
        raise RuntimeError("network down")

    monkeypatch.setattr(c, "_send_chat_request", fake_send)
    res = c.attempt_continuation(original_prompt="orig", partial_content="partial")
    assert res.success is False
    assert res.final_done_reason == "error"


def test_continuation_attempt_with_logging(tmp_path, monkeypatch) -> None:
    from software_engineering_team.shared.continuation import ResponseContinuator

    c = ResponseContinuator(base_url="http://x", model="m", max_cycles=2)

    def fake_send(messages, json_mode=False):
        return ("done", "stop")

    monkeypatch.setattr(c, "_send_chat_request", fake_send)
    c.attempt_continuation(
        original_prompt="orig",
        partial_content="part",
        task_id="task1",
        project_root=tmp_path,
    )
    files = list((tmp_path / "continuation_logs").glob("*.txt"))
    assert files


def test_attempt_response_continuation_helper(monkeypatch) -> None:
    from software_engineering_team.shared import continuation

    class _Fake:
        def attempt_continuation(self, **kwargs):
            return continuation.ContinuationResult(success=True, content="ok", cycles_used=1)

    monkeypatch.setattr(continuation, "ResponseContinuator", lambda **_: _Fake())
    r = continuation.attempt_response_continuation(
        base_url="http://x", model="m", original_prompt="o", partial_content="p"
    )
    assert r.success is True


# ---------------------------------------------------------------------------
# decomposition
# ---------------------------------------------------------------------------


def test_decomposition_context_basics() -> None:
    from software_engineering_team.shared.decomposition import DecompositionContext

    ctx = DecompositionContext(original_task="task", original_content="content")
    assert ctx.depth == 0
    assert ctx.can_decompose() is True
    assert ctx.get_decomposition_path() == "root"

    ctx.add_partial_response("x")
    ctx.mark_continuation_attempted()
    assert ctx.continuation_attempted is True


def test_decomposition_context_can_decompose_false() -> None:
    from software_engineering_team.shared.decomposition import DecompositionContext

    ctx = DecompositionContext(original_task="t", depth=5, max_depth=5)
    assert ctx.can_decompose() is False


def test_decomposition_context_create_child() -> None:
    from software_engineering_team.shared.decomposition import DecompositionContext

    parent = DecompositionContext(original_task="t")
    parent.add_partial_response("x")
    child = parent.create_child(0, 3)
    assert child.depth == 1
    assert child.parent_context is parent
    assert "depth_0_chunk_1_of_3" in child.get_decomposition_path()


def test_decomposition_log_decomposition(caplog) -> None:
    import logging

    from software_engineering_team.shared.decomposition import DecompositionContext

    ctx = DecompositionContext(original_task="t")
    with caplog.at_level(logging.INFO):
        ctx.log_decomposition("MyAgent", 5)
    assert any("MyAgent" in r.message for r in caplog.records)


def test_section_decomposition_splits_by_h2() -> None:
    from software_engineering_team.shared.decomposition import (
        DecompositionContext,
        SectionDecompositionStrategy,
    )

    s = SectionDecompositionStrategy()
    ctx = DecompositionContext(original_task="t")
    chunks = s.decompose("## A\nbody1\n## B\nbody2", ctx)
    assert len(chunks) == 2


def test_section_decomposition_splits_by_h1() -> None:
    from software_engineering_team.shared.decomposition import (
        DecompositionContext,
        SectionDecompositionStrategy,
    )

    s = SectionDecompositionStrategy()
    ctx = DecompositionContext(original_task="t")
    chunks = s.decompose("# A\nbody1\n# B\nbody2", ctx)
    assert len(chunks) == 2


def test_section_decomposition_chunks_by_size() -> None:
    from software_engineering_team.shared.decomposition import (
        DecompositionContext,
        SectionDecompositionStrategy,
    )

    s = SectionDecompositionStrategy(chunk_size=10)
    ctx = DecompositionContext(original_task="t")
    chunks = s.decompose("x" * 35, ctx)
    assert len(chunks) == 4


def test_section_decomposition_empty_content() -> None:
    from software_engineering_team.shared.decomposition import (
        DecompositionContext,
        SectionDecompositionStrategy,
    )

    s = SectionDecompositionStrategy()
    ctx = DecompositionContext(original_task="t")
    assert s.decompose("", ctx) == []
    assert s.decompose("   ", ctx) == ["   "]


def test_section_decomposition_merge() -> None:
    from software_engineering_team.shared.decomposition import (
        SectionDecompositionStrategy,
    )

    s = SectionDecompositionStrategy()
    a = {"list": [1], "obj": {"a": 1}, "single": "x", "empty": ""}
    b = {"list": [2], "obj": {"b": 2}, "single": "y", "empty": "now-set"}
    merged = s.merge([a, b])
    assert merged["list"] == [1, 2]
    assert merged["obj"] == {"a": 1, "b": 2}
    assert merged["single"] == "x"
    assert merged["empty"] == "now-set"
    assert s.merge([]) == {}
    assert s.merge(["not a dict"]) == {}  # type: ignore[list-item]


def test_section_decomposition_create_chunk_prompt() -> None:
    from software_engineering_team.shared.decomposition import (
        SectionDecompositionStrategy,
    )

    s = SectionDecompositionStrategy()
    prompt = s.create_chunk_prompt("orig", "CHUNK", 0, 3)
    assert "chunk 1 of 3" in prompt
    assert "CHUNK" in prompt


def test_file_based_decomposition_splits_by_files() -> None:
    from software_engineering_team.shared.decomposition import (
        DecompositionContext,
        FileBasedDecompositionStrategy,
    )

    s = FileBasedDecompositionStrategy()
    ctx = DecompositionContext(original_task="t")
    content = """
- `src/foo.py`
- `src/bar.py`
"""
    chunks = s.decompose(content, ctx)
    assert len(chunks) >= 2


def test_file_based_decomposition_falls_back() -> None:
    from software_engineering_team.shared.decomposition import (
        DecompositionContext,
        FileBasedDecompositionStrategy,
    )

    s = FileBasedDecompositionStrategy()
    ctx = DecompositionContext(original_task="t")
    # No files mentioned, single file, or sections — falls back to SectionStrategy
    chunks = s.decompose("just text", ctx)
    assert isinstance(chunks, list)


def test_file_based_decomposition_merge() -> None:
    from software_engineering_team.shared.decomposition import (
        FileBasedDecompositionStrategy,
    )

    s = FileBasedDecompositionStrategy()
    merged = s.merge([{"a.py": "x"}, {"b.py": "y"}, "not dict"])  # type: ignore[list-item]
    assert merged == {"a.py": "x", "b.py": "y"}


def test_recursive_processor_happy_path() -> None:
    from software_engineering_team.shared.decomposition import (
        RecursiveProcessor,
        SectionDecompositionStrategy,
    )

    p: RecursiveProcessor = RecursiveProcessor(SectionDecompositionStrategy())
    out = p.process(
        llm=MagicMock(),
        prompt="hi",
        content="hi",
        agent_name="A",
        process_fn=lambda prompt: {"got": prompt},
    )
    assert out == {"got": "hi"}


def test_recursive_processor_max_depth_post_mortem(tmp_path, monkeypatch) -> None:
    # Patch write_post_mortem so it doesn't pollute cwd
    import software_engineering_team.shared.post_mortem as pm
    from llm_service import LLMTruncatedError
    from software_engineering_team.shared.decomposition import (
        DecompositionContext,
        RecursiveProcessor,
        SectionDecompositionStrategy,
    )

    written = []

    def fake_write(**kwargs):
        written.append(kwargs)
        return tmp_path / "POST_MORTEMS.md"

    monkeypatch.setattr(pm, "write_post_mortem", fake_write)

    def fail(_prompt):
        raise LLMTruncatedError("truncated", partial_content="part")

    p: RecursiveProcessor = RecursiveProcessor(SectionDecompositionStrategy(), max_depth=0)
    ctx = DecompositionContext(original_task="t", max_depth=0)
    with pytest.raises(LLMTruncatedError):
        p.process(
            llm=MagicMock(),
            prompt="x",
            content="x",
            agent_name="A",
            process_fn=fail,
            context=ctx,
        )


def test_recursive_processor_decomposes_then_merges(monkeypatch) -> None:
    from llm_service import LLMTruncatedError
    from software_engineering_team.shared.decomposition import (
        DecompositionContext,
        RecursiveProcessor,
        SectionDecompositionStrategy,
    )

    calls = {"n": 0}

    def fn(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMTruncatedError("trunc", partial_content="part")
        return {"items": [prompt[:5]]}

    s = SectionDecompositionStrategy()
    p: RecursiveProcessor = RecursiveProcessor(s, max_depth=3)
    ctx = DecompositionContext(
        original_task="t",
        original_content="## a\nA\n## b\nB",
        max_depth=3,
    )
    out = p.process(
        llm=MagicMock(),
        prompt="hi",
        content="## a\nA\n## b\nB",
        agent_name="A",
        process_fn=fn,
        context=ctx,
    )
    assert "items" in out


def test_recursive_processor_chunk_failure_isolated(monkeypatch) -> None:
    from llm_service import LLMTruncatedError
    from software_engineering_team.shared.decomposition import (
        DecompositionContext,
        RecursiveProcessor,
        SectionDecompositionStrategy,
    )

    state = {"first": True}

    def fn(prompt):
        if state["first"]:
            state["first"] = False
            raise LLMTruncatedError("trunc", partial_content="part")
        # Simulate one chunk failing entirely
        if "## b" in prompt:
            raise RuntimeError("chunk failed")
        return {"items": ["ok"]}

    s = SectionDecompositionStrategy()
    p: RecursiveProcessor = RecursiveProcessor(s, max_depth=3)
    ctx = DecompositionContext(
        original_task="t",
        original_content="## a\nA\n## b\nB",
        max_depth=3,
    )
    out = p.process(
        llm=MagicMock(),
        prompt="hi",
        content="## a\nA\n## b\nB",
        agent_name="A",
        process_fn=fn,
        context=ctx,
    )
    # Either succeeded chunks merged or empty
    assert isinstance(out, dict)


def test_recursive_processor_single_chunk_returns_empty_merge(monkeypatch) -> None:
    from llm_service import LLMTruncatedError
    from software_engineering_team.shared.decomposition import (
        DecompositionContext,
        RecursiveProcessor,
        SectionDecompositionStrategy,
    )

    class _SingleStrategy(SectionDecompositionStrategy):
        def decompose(self, content, context):
            return ["onlychunk"]

    def fn(prompt):
        raise LLMTruncatedError("trunc", partial_content="part")

    p: RecursiveProcessor = RecursiveProcessor(_SingleStrategy(), max_depth=3)
    ctx = DecompositionContext(original_task="t", max_depth=3)
    out = p.process(
        llm=MagicMock(),
        prompt="hi",
        content="text",
        agent_name="A",
        process_fn=fn,
        context=ctx,
    )
    assert out == {}


def test_process_with_decomposition_helper() -> None:
    # The helper imports inside process(); easier to monkeypatch strands.Agent directly.
    import strands

    from software_engineering_team.shared.decomposition import (
        SectionDecompositionStrategy,
        process_with_decomposition,
    )

    orig_agent = strands.Agent

    class _DummyAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return '{"hello": "world"}'

    strands.Agent = _DummyAgent
    try:
        out = process_with_decomposition(
            llm=MagicMock(),
            prompt="hi",
            content="hi",
            agent_name="X",
            strategy=SectionDecompositionStrategy(),
        )
        assert out == {"hello": "world"}
    finally:
        strands.Agent = orig_agent


def test_process_with_decomposition_helper_recovers_fenced_json(monkeypatch) -> None:
    import json

    import strands

    from software_engineering_team.shared.decomposition import (
        SectionDecompositionStrategy,
        process_with_decomposition,
    )

    payload = {"hello": "world"}
    fenced = "```json\n" + json.dumps(payload) + "\n```"

    class _FencedAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return fenced

    monkeypatch.setattr(strands, "Agent", _FencedAgent)

    out = process_with_decomposition(
        llm=MagicMock(),
        prompt="hi",
        content="hi",
        agent_name="X",
        strategy=SectionDecompositionStrategy(),
    )
    assert out == payload


def test_recursive_processor_default_branch_uses_injected_llm_client(monkeypatch) -> None:
    """Regression test: the default branch (no ``process_fn``) must resolve the
    Strands model through the caller's injected ``llm`` client instead of
    silently discarding it and building a default model with no client
    threaded through (the pre-fix bug).

    Asserts against the ``get_strands_model`` call contract rather than a
    concrete client/model class, so it stays pinned to the ``LLMClient``
    protocol: the pre-fix code called ``get_strands_model()`` with no
    ``client`` kwarg at all, so this would fail against the old behavior.
    """
    import strands

    import llm_service.strands_model as strands_model_module
    from llm_service import LLMClient
    from software_engineering_team.shared.decomposition import (
        RecursiveProcessor,
        SectionDecompositionStrategy,
    )

    captured_models = []
    captured_get_strands_model_calls = []

    class _RecordingAgent:
        def __init__(self, model=None, **kw):
            captured_models.append(model)

        def __call__(self, prompt):
            return '{"ok": true}'

    sentinel_model = object()

    def _fake_get_strands_model(*args, **kwargs):
        captured_get_strands_model_calls.append((args, kwargs))
        return sentinel_model

    monkeypatch.setattr(strands, "Agent", _RecordingAgent)
    monkeypatch.setattr(strands_model_module, "get_strands_model", _fake_get_strands_model)

    client = MagicMock(spec=LLMClient)
    p: RecursiveProcessor = RecursiveProcessor(SectionDecompositionStrategy())
    out = p.process(llm=client, prompt="hi", content="hi", agent_name="A")

    assert out == {"ok": True}
    assert len(captured_get_strands_model_calls) == 1
    _, kwargs = captured_get_strands_model_calls[0]
    assert kwargs.get("client") is client
    assert kwargs.get("response_format") == "json"
    assert captured_models == [sentinel_model]
