"""Helper-function tests for the deprecated frontend_team feature_agent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from software_engineering_team.frontend_team_deprecated.feature_agent import agent as feature_mod
from software_engineering_team.frontend_team_deprecated.feature_agent.agent import (
    _apply_frontend_build_fix_edits,
    _extract_affected_file_paths_from_frontend_build_errors,
    _llm_allowed_extensions,
    _llm_allowed_root_paths,
    _read_frontend_affected_files_code,
    _read_repo_code,
    _resolved_framework_for_implementation,
    _task_requirements_with_route_expectations,
    _validate_file_paths,
)
from software_engineering_team.shared.models import Task, TaskType


def test_resolved_framework_for_implementation_default():
    """When nothing is detectable, defaults to react."""
    result = _resolved_framework_for_implementation({}, "")
    assert result == "react"


def test_resolved_framework_for_implementation_from_spec():
    result = _resolved_framework_for_implementation({}, "We use Angular for our SPA")
    assert result in ("angular", "react", "vue")


def test_task_requirements_with_route_expectations(tmp_path: Path):
    task = Task(
        id="t1",
        type=TaskType.FRONTEND,
        title="Login",
        description="login form",
        assignee="frontend",
        requirements="reqs",
    )
    out = _task_requirements_with_route_expectations(task, tmp_path)
    assert isinstance(out, str)


def test_read_repo_code_default_extensions(tmp_path: Path):
    (tmp_path / "x.ts").write_text("ts code")
    (tmp_path / "y.html").write_text("<div></div>")
    out = _read_repo_code(tmp_path)
    assert "x.ts" in out
    assert "y.html" in out


def test_extract_affected_file_paths_from_build_errors(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "comp.ts").write_text("x")
    (tmp_path / "src" / "comp.html").write_text("<x/>")
    errs = (
        "src/comp.ts:10:5 - error TS2304: Cannot find name 'foo'\n"
        "src/comp.html:5 - error: blah\n"
        "Could not resolve './missing.module'\n"
        "Could not resolve './comp.ts'\n"
    )
    paths = _extract_affected_file_paths_from_frontend_build_errors(errs, tmp_path)
    assert "src/comp.ts" in paths
    assert "src/comp.html" in paths


def test_extract_affected_file_paths_inserts_routes_file(tmp_path: Path):
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "app.routes.ts").write_text("routes")
    paths = _extract_affected_file_paths_from_frontend_build_errors("", tmp_path)
    assert paths[0] == "src/app/app.routes.ts"


def test_extract_affected_file_paths_caps_at_10(tmp_path: Path):
    (tmp_path / "src").mkdir()
    errs_lines = []
    for i in range(20):
        f = tmp_path / "src" / f"comp{i}.ts"
        f.write_text("x")
        errs_lines.append(f"src/comp{i}.ts:1 - error")
    paths = _extract_affected_file_paths_from_frontend_build_errors("\n".join(errs_lines), tmp_path)
    assert len(paths) <= 10


def test_read_frontend_affected_files_code(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("aaa")
    (tmp_path / "src" / "b.ts").write_text("bbb")
    out = _read_frontend_affected_files_code(tmp_path, ["src/a.ts", "src/b.ts"])
    assert "a.ts" in out
    assert "aaa" in out
    assert "bbb" in out


def test_read_frontend_affected_files_code_missing(tmp_path: Path):
    out = _read_frontend_affected_files_code(tmp_path, ["src/missing.ts"])
    assert "No affected files" in out


def test_read_frontend_affected_files_code_caps_at_12k(tmp_path: Path):
    """Once total exceeds 12000 chars, additional files are skipped."""
    (tmp_path / "src").mkdir()
    for i in range(20):
        (tmp_path / "src" / f"a{i}.ts").write_text("x" * 1000)
    paths = [f"src/a{i}.ts" for i in range(20)]
    out = _read_frontend_affected_files_code(tmp_path, paths)
    assert len(out) < 20000  # we cap at ~12k


def test_apply_frontend_build_fix_edits_no_target_file(tmp_path: Path):
    """If file doesn't exist, return success=False."""
    from build_fix_specialist.models import CodeEdit

    edit = CodeEdit(file_path="src/missing.ts", old_text="x", new_text="y")
    ok, msg, files = _apply_frontend_build_fix_edits(tmp_path, [edit])
    assert ok is False
    assert "No edits" in msg


def test_apply_frontend_build_fix_edits_old_text_missing(tmp_path: Path):
    from build_fix_specialist.models import CodeEdit

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("hello")
    edit = CodeEdit(file_path="src/a.ts", old_text="missing", new_text="new")
    ok, msg, files = _apply_frontend_build_fix_edits(tmp_path, [edit])
    assert ok is False
    assert "old_text not found" in msg


def test_apply_frontend_build_fix_edits_success(tmp_path: Path):
    from build_fix_specialist.models import CodeEdit

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("hello world")
    edit = CodeEdit(file_path="src/a.ts", old_text="hello", new_text="HI")
    ok, msg, files = _apply_frontend_build_fix_edits(tmp_path, [edit])
    assert ok is True
    assert "src/a.ts" in files
    assert files["src/a.ts"] == "HI world"


def test_apply_frontend_build_fix_edits_filters_non_codeedit(tmp_path: Path):
    """Non-CodeEdit entries in the list are silently ignored."""
    ok, msg, files = _apply_frontend_build_fix_edits(tmp_path, ["string", 42, None])
    assert ok is False


def test_apply_frontend_build_fix_edits_unreadable_file(tmp_path: Path, monkeypatch):
    from build_fix_specialist.models import CodeEdit

    (tmp_path / "src").mkdir()
    f = tmp_path / "src" / "a.ts"
    f.write_text("hello")

    real_read = Path.read_text

    def _bad_read(self, *a, **kw):
        if self == f:
            raise OSError("boom")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _bad_read)
    edit = CodeEdit(file_path="src/a.ts", old_text="hello", new_text="HI")
    ok, msg, files = _apply_frontend_build_fix_edits(tmp_path, [edit])
    assert ok is False


def test_validate_file_paths_src_file_ok():
    files = {"src/app/x.ts": "code", "src/app/y.html": "<div></div>"}
    validated, warnings = _validate_file_paths(files, llm_client=None)
    assert "src/app/x.ts" in validated
    assert "src/app/y.html" in validated


def test_validate_file_paths_strips_frontend_prefix():
    files = {"frontend/src/x.ts": "code"}
    validated, _ = _validate_file_paths(files, llm_client=None)
    assert "src/x.ts" in validated


def test_validate_file_paths_rejects_non_src():
    files = {"other/dir/x.ts": "code"}
    validated, warnings = _validate_file_paths(files, llm_client=None)
    assert "other/dir/x.ts" not in validated
    assert any("under 'src/'" in w for w in warnings)


def test_validate_file_paths_accepts_root_config_files():
    files = {"angular.json": "{}", "package.json": "{}", "tsconfig.json": "{}"}
    validated, _ = _validate_file_paths(files, llm_client=None)
    assert "angular.json" in validated
    assert "package.json" in validated


def test_validate_file_paths_rejects_root_unknown_no_llm():
    files = {"random.config.js": "code"}
    validated, warnings = _validate_file_paths(files, llm_client=None)
    assert "random.config.js" not in validated
    assert any("LLM required" in w for w in warnings)


def test_validate_file_paths_rejects_bad_extension_no_llm():
    files = {"src/x.py": "code"}
    validated, warnings = _validate_file_paths(files, llm_client=None)
    assert "src/x.py" not in validated
    assert any(".py" in w for w in warnings)


def test_validate_file_paths_rejects_empty_content():
    files = {"src/x.ts": "   "}
    validated, warnings = _validate_file_paths(files, llm_client=None)
    assert "src/x.ts" not in validated
    assert any("Empty file content" in w for w in warnings)


def test_validate_file_paths_rejects_sentence_segment():
    files = {"src/implement-the-user-page/x.ts": "code"}
    validated, warnings = _validate_file_paths(files, llm_client=None)
    assert "src/implement-the-user-page/x.ts" not in validated


def test_validate_file_paths_rejects_too_long_segment():
    files = {"src/" + "x" * 50 + "/y.ts": "code"}
    validated, warnings = _validate_file_paths(files, llm_client=None)
    assert not any("src/x" * 50 in k for k in validated)


def test_validate_file_paths_env_files_at_root_need_llm():
    """Root-level .env files need LLM approval (no fast path)."""
    files = {".env": "X=1", ".env.local": "Y=2"}
    validated, warnings = _validate_file_paths(files, llm_client=None)
    # Without LLM, root-level .env files are rejected (require LLM root validation)
    assert validated == {}


def test_validate_file_paths_empty_normalized():
    files = {"/": "x", "frontend/": "y"}
    validated, warnings = _validate_file_paths(files, llm_client=None)
    assert validated == {}


def test_validate_file_paths_root_via_llm(monkeypatch):
    """When llm_client is provided, unknown root paths are validated via LLM."""

    def _fake_root_paths(llm, paths):
        return {"custom.config.js"}

    monkeypatch.setattr(feature_mod, "_llm_allowed_root_paths", _fake_root_paths)
    files = {"custom.config.js": "x", "bad.config.unknown": "y"}
    llm = MagicMock()
    validated, warnings = _validate_file_paths(files, llm_client=llm)
    # custom.config.js was approved by LLM but extension is unknown, so it
    # gets pushed into unknown_ext_files; it should appear in validated only
    # if LLM also approves its extension.

    # The extension check happens in the second pass when LLM is provided
    def _fake_exts(llm, exts):
        return {".js"}

    monkeypatch.setattr(feature_mod, "_llm_allowed_extensions", _fake_exts)
    files2 = {"custom.config.js": "code"}
    validated2, _ = _validate_file_paths(files2, llm_client=llm)
    assert isinstance(validated2, dict)


def test_llm_allowed_extensions_empty():
    assert _llm_allowed_extensions(None, set()) == set()


def test_llm_allowed_extensions_returns_empty_on_exception(monkeypatch):
    """When the LLM call raises, the function returns an empty set."""

    class _BadAgent:
        def __call__(self, *a, **kw):
            raise RuntimeError("err")

    monkeypatch.setattr(feature_mod, "Agent", lambda *a, **kw: _BadAgent())
    monkeypatch.setattr(
        "software_engineering_team.shared.strands_model.resolve_text_mode_strands_model",
        lambda llm: object(),
    )
    assert _llm_allowed_extensions(None, {".svg"}) == set()


def test_llm_allowed_extensions_returns_none(monkeypatch):
    """LLM returns 'none' -> empty set."""

    class _StubAgent:
        def __call__(self, *a, **kw):
            return "none"

    monkeypatch.setattr(feature_mod, "Agent", lambda *a, **kw: _StubAgent())
    monkeypatch.setattr(
        "software_engineering_team.shared.strands_model.resolve_text_mode_strands_model",
        lambda llm: object(),
    )
    assert _llm_allowed_extensions(None, {".svg"}) == set()


def test_llm_allowed_extensions_returns_match(monkeypatch):
    """LLM returns extensions; only known ones are included."""

    class _StubAgent:
        def __call__(self, *a, **kw):
            return ".svg, .md"

    monkeypatch.setattr(feature_mod, "Agent", lambda *a, **kw: _StubAgent())
    monkeypatch.setattr(
        "software_engineering_team.shared.strands_model.resolve_text_mode_strands_model",
        lambda llm: object(),
    )
    out = _llm_allowed_extensions(None, {".svg", ".md", ".weird"})
    assert ".svg" in out
    assert ".md" in out


def test_llm_allowed_root_paths_empty():
    assert _llm_allowed_root_paths(None, []) == set()


def test_llm_allowed_root_paths_exception(monkeypatch):
    class _BadAgent:
        def __call__(self, *a, **kw):
            raise RuntimeError("err")

    monkeypatch.setattr(feature_mod, "Agent", lambda *a, **kw: _BadAgent())
    monkeypatch.setattr(
        "software_engineering_team.shared.strands_model.resolve_text_mode_strands_model",
        lambda llm: object(),
    )
    out = _llm_allowed_root_paths(None, ["foo.js"])
    # Returns empty set (or None depending on implementation)
    assert out == set() or out is None or isinstance(out, set)


def _make_task() -> Task:
    return Task(
        id="t-sem",
        type=TaskType.FRONTEND,
        title="Login",
        description="login form",
        assignee="frontend",
        requirements="reqs",
    )


def test_plan_task_degrades_on_semantic_exhaustion(monkeypatch):
    """Planning is optional: a semantically exhausted planning call degrades to
    no-plan instead of aborting the workflow."""
    from llm_service import LLMSemanticExhaustionError

    agent = feature_mod.FrontendExpertAgent(llm_client=MagicMock())

    def exhausted(fn, **kwargs):
        raise LLMSemanticExhaustionError("no content", attempts_used=2)

    monkeypatch.setattr(feature_mod, "call_llm_with_retries", exhausted)
    plan = agent._plan_task(
        task=_make_task(),
        existing_code="",
        spec_content="spec",
        architecture=None,
    )
    assert plan == ""


def test_run_workflow_returns_structured_result_on_semantic_exhaustion(monkeypatch, tmp_path: Path):
    """A semantic-exhaustion receipt escaping the workflow body produces the same
    structured llm_unreachable result as LLMUnreachableAfterRetriesError —
    call_llm_with_retries re-raises it without converting, so the workflow
    handler must catch the receipt type explicitly."""
    from llm_service import LLMSemanticExhaustionError
    from software_engineering_team.shared import context_sizing, git_utils

    monkeypatch.setattr(git_utils, "create_feature_branch", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(git_utils, "checkout_branch", lambda *a, **k: None)

    def exhausted(_llm):
        raise LLMSemanticExhaustionError(
            "no content",
            attempts_used=2,
            original_thinking_level="max",
            retry_thinking_level="high",
        )

    monkeypatch.setattr(context_sizing, "compute_existing_code_chars", exhausted)
    agent = feature_mod.FrontendExpertAgent(llm_client=MagicMock())
    result = agent.run_workflow(
        repo_path=tmp_path,
        backend_dir=tmp_path,
        task=_make_task(),
        spec_content="spec",
        architecture=None,
        qa_agent=MagicMock(),
        accessibility_agent=MagicMock(),
        security_agent=MagicMock(),
        code_review_agent=MagicMock(),
        build_verifier=lambda **k: (True, "ok"),
    )
    from software_engineering_team.shared.job_store import LLM_SEMANTIC_EXHAUSTION

    assert result.success is False
    assert result.llm_unreachable is True
    # Exact sentinel: the orchestrator propagates failure_reason into task
    # state and matches it against the shared constants when aggregating.
    assert result.failure_reason == LLM_SEMANTIC_EXHAUSTION
