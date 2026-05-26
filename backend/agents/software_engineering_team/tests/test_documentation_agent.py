"""Unit tests for DocumentationAgent."""

from __future__ import annotations

import json
from pathlib import Path

from software_engineering_team.technical_writers.documentation_agent import agent as doc_agent_mod
from software_engineering_team.technical_writers.documentation_agent.agent import (
    MAX_CODEBASE_CHARS,
    DocumentationAgent,
)
from software_engineering_team.technical_writers.documentation_agent.models import (
    DocumentationInput,
    DocumentationOutput,
    DocumentationStatus,
)


class _FakeAgent:
    """Simulates a strands Agent. Returns canned JSON for sequential calls."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    def __call__(self, prompt):  # noqa: D401
        self.calls.append(prompt)
        if self._payloads:
            return json.dumps(self._payloads.pop(0))
        return "{}"


def _patch_agent(monkeypatch, payloads):
    fake = _FakeAgent(payloads)

    def _factory(*args, **kwargs):
        return fake

    monkeypatch.setattr(doc_agent_mod, "Agent", _factory)
    monkeypatch.setattr(doc_agent_mod, "get_strands_model", lambda _key=None: object())
    return fake


def test_doc_agent_init_uses_strands_model(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(doc_agent_mod, "get_strands_model", lambda key: sentinel)
    a = DocumentationAgent()
    assert a._model is sentinel


def test_doc_agent_init_accepts_strands_model_instance(monkeypatch) -> None:
    from strands.models.model import Model as StrandsModel

    class _FakeModel(StrandsModel):
        def __init__(self):
            pass

        def structured_output(self, *a, **kw):  # pragma: no cover - shim
            return {}

        def update_config(self, *a, **kw):
            pass

        def get_config(self):
            return {}

        async def stream(self, *a, **kw):  # pragma: no cover
            yield {}

    m = _FakeModel()
    a = DocumentationAgent(llm_client=m)
    assert a._model is m


def test_doc_agent_run_happy_path(monkeypatch) -> None:
    """Both LLM calls (README + CONTRIBUTORS) succeed and changes are recorded."""
    _patch_agent(
        monkeypatch,
        [
            {
                "readme_content": "# Project\n",
                "readme_changed": True,
                "frontend_readme": "# FE\n",
                "frontend_readme_changed": True,
                "backend_readme": "# BE\n",
                "backend_readme_changed": True,
                "devops_readme": "# DO\n",
                "devops_readme_changed": True,
                "summary": "Added README",
                "suggested_commit_message": "docs: update readme",
            },
            {
                "contributors_content": "* alice",
                "contributors_changed": True,
                "summary": "Added contributors",
            },
        ],
    )

    a = DocumentationAgent()
    out = a.run(
        DocumentationInput(
            repo_path="/tmp/x",
            task_id="t1",
            task_summary="did stuff",
            agent_type="backend",
            codebase_content="def foo(): pass",
            existing_readme="old",
            existing_readme_frontend="oldfe",
            existing_readme_backend="oldbe",
            existing_readme_devops="olddo",
            existing_contributors="* old",
            has_frontend_folder=True,
            has_backend_folder=True,
            has_devops_folder=True,
        )
    )
    assert out.readme_changed
    assert out.readme_frontend_changed
    assert out.readme_backend_changed
    assert out.readme_devops_changed
    assert out.contributors_changed
    assert "Added README" in out.summary
    assert out.suggested_commit_message == "docs: update readme"


def test_doc_agent_run_truncates_long_codebase(monkeypatch) -> None:
    fake = _patch_agent(
        monkeypatch,
        [
            {"readme_content": "", "readme_changed": False, "summary": "no-op"},
            {"contributors_content": "", "contributors_changed": False, "summary": "no-op"},
        ],
    )
    huge = "x" * (MAX_CODEBASE_CHARS + 5000)
    a = DocumentationAgent()
    a.run(
        DocumentationInput(
            repo_path="/tmp/x",
            task_id="t1",
            codebase_content=huge,
        )
    )
    assert any("truncated" in c for c in fake.calls)


def test_doc_agent_run_forces_creation_when_no_readme_existed(monkeypatch) -> None:
    """If README didn't exist and LLM returned content, readme_changed is forced True."""
    _patch_agent(
        monkeypatch,
        [
            {"readme_content": "# new\n", "readme_changed": False, "summary": "create"},
            {"contributors_content": "", "contributors_changed": False, "summary": "no-op"},
        ],
    )
    a = DocumentationAgent()
    out = a.run(
        DocumentationInput(
            repo_path="/tmp/x",
            task_id="t1",
            existing_readme="",  # missing
        )
    )
    assert out.readme_changed is True
    assert out.readme_content == "# new\n"


def test_doc_agent_run_detects_real_diff(monkeypatch) -> None:
    """When LLM returns readme_changed=False but text actually differs, forces True."""
    _patch_agent(
        monkeypatch,
        [
            {"readme_content": "different", "readme_changed": False, "summary": "actually changed"},
            {"contributors_content": "", "contributors_changed": False, "summary": "no-op"},
        ],
    )
    a = DocumentationAgent()
    out = a.run(
        DocumentationInput(
            repo_path="/tmp/x",
            task_id="t1",
            existing_readme="original",
        )
    )
    assert out.readme_changed is True


def test_doc_agent_run_readme_llm_error_returns_partial(monkeypatch) -> None:
    """README LLM throws; we still try CONTRIBUTORS and return partial output."""

    class _ErrorReadmeFake:
        def __init__(self):
            self.calls = 0

        def __call__(self, prompt):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            return json.dumps(
                {
                    "contributors_content": "* alice",
                    "contributors_changed": True,
                    "summary": "ok",
                }
            )

    fake = _ErrorReadmeFake()
    monkeypatch.setattr(doc_agent_mod, "Agent", lambda *a, **kw: fake)
    monkeypatch.setattr(doc_agent_mod, "get_strands_model", lambda _k=None: object())

    a = DocumentationAgent()
    out = a.run(DocumentationInput(repo_path="/tmp/x", task_id="t1"))
    assert "README update skipped" in out.summary
    assert out.contributors_changed is True


def test_doc_agent_run_contributors_llm_error(monkeypatch) -> None:
    class _ErrFake:
        def __init__(self):
            self.calls = 0

        def __call__(self, prompt):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        "readme_content": "ok",
                        "readme_changed": True,
                        "summary": "readme good",
                    }
                )
            raise RuntimeError("bad json")

    fake = _ErrFake()
    monkeypatch.setattr(doc_agent_mod, "Agent", lambda *a, **kw: fake)
    monkeypatch.setattr(doc_agent_mod, "get_strands_model", lambda _k=None: object())

    a = DocumentationAgent()
    out = a.run(DocumentationInput(repo_path="/tmp/x", task_id="t1"))
    assert "CONTRIBUTORS check skipped" in out.summary
    assert out.readme_changed


def test_doc_agent_final_review_appends_suffix(monkeypatch) -> None:
    """When is_final_review=True, prompts include the FINAL_REVIEW suffixes."""
    fake = _patch_agent(
        monkeypatch,
        [
            {"readme_content": "x", "readme_changed": True, "summary": "f"},
            {
                "contributors_content": "* x",
                "contributors_changed": True,
                "summary": "f",
            },
        ],
    )
    a = DocumentationAgent()
    a.run(
        DocumentationInput(
            repo_path="/tmp/x",
            task_id="t1",
            is_final_review=True,
            completed_task_ids=["a", "b"],
            existing_readme="prev",
        )
    )
    # second call should mention the completed-tasks list
    assert any("a, b" in c for c in fake.calls)


def test_doc_agent_run_full_workflow_no_changes(monkeypatch, tmp_path: Path) -> None:
    """No-change path: branch created, generate, no files to write, cleanup."""
    _patch_agent(
        monkeypatch,
        [
            {"readme_content": "", "readme_changed": False, "summary": "nothing"},
            {"contributors_content": "", "contributors_changed": False, "summary": "nothing"},
        ],
    )

    monkeypatch.setattr(
        doc_agent_mod, "create_feature_branch", lambda *a, **kw: (True, "docs/t1")
    )
    monkeypatch.setattr(doc_agent_mod, "checkout_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(doc_agent_mod, "delete_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(doc_agent_mod, "merge_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(
        doc_agent_mod, "write_files_and_commit", lambda *a, **kw: (True, "")
    )

    statuses = []

    def on_status(s, d):
        statuses.append((s, d))

    a = DocumentationAgent()
    out = a.run_full_workflow(
        repo_path=tmp_path,
        task_id="t1",
        task_summary="s",
        agent_type="backend",
        spec_content="",
        architecture=None,
        codebase_content="code",
        on_status=on_status,
    )
    assert isinstance(out, DocumentationOutput)
    assert any(s == DocumentationStatus.COMPLETE for s, _ in statuses)


def test_doc_agent_run_full_workflow_branch_create_fails(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        doc_agent_mod, "create_feature_branch", lambda *a, **kw: (False, "no perms")
    )
    a = DocumentationAgent()
    out = a.run_full_workflow(
        repo_path=tmp_path,
        task_id="t1",
        task_summary="s",
        agent_type="backend",
        spec_content="",
        architecture=None,
        codebase_content="code",
    )
    assert "branch creation failed" in out.summary


def test_doc_agent_run_full_workflow_writes_and_merges(monkeypatch, tmp_path: Path) -> None:
    """Happy path: files written, commit, merge OK."""
    _patch_agent(
        monkeypatch,
        [
            {
                "readme_content": "# Hi\n",
                "readme_changed": True,
                "frontend_readme": "# FE",
                "frontend_readme_changed": True,
                "backend_readme": "# BE",
                "backend_readme_changed": True,
                "devops_readme": "# DO",
                "devops_readme_changed": True,
                "summary": "ok",
                "suggested_commit_message": "docs: update",
            },
            {
                "contributors_content": "* alice",
                "contributors_changed": True,
                "summary": "ok",
            },
        ],
    )

    # Create subfolders so the path checks pass
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / "devops").mkdir()

    monkeypatch.setattr(
        doc_agent_mod, "create_feature_branch", lambda *a, **kw: (True, "docs/t1")
    )
    write_calls = []
    monkeypatch.setattr(
        doc_agent_mod,
        "write_files_and_commit",
        lambda path, files, msg: (write_calls.append((files, msg)) or (True, "")),
    )
    monkeypatch.setattr(doc_agent_mod, "merge_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(doc_agent_mod, "delete_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(doc_agent_mod, "checkout_branch", lambda *a, **kw: (True, ""))

    a = DocumentationAgent()
    out = a.run_full_workflow(
        repo_path=tmp_path,
        task_id="t1",
        task_summary="s",
        agent_type="backend",
        spec_content="",
        architecture=None,
        codebase_content="code",
    )
    assert out.readme_changed
    assert write_calls
    files = write_calls[0][0]
    assert "README.md" in files
    assert "frontend/README.md" in files
    assert "backend/README.md" in files
    assert "devops/README.md" in files
    assert "CONTRIBUTORS.md" in files


def test_doc_agent_run_full_workflow_commit_fails(monkeypatch, tmp_path: Path) -> None:
    _patch_agent(
        monkeypatch,
        [
            {"readme_content": "x", "readme_changed": True, "summary": "ok"},
            {"contributors_content": "", "contributors_changed": False, "summary": "ok"},
        ],
    )
    monkeypatch.setattr(
        doc_agent_mod, "create_feature_branch", lambda *a, **kw: (True, "docs/t1")
    )
    monkeypatch.setattr(
        doc_agent_mod, "write_files_and_commit", lambda *a, **kw: (False, "no diff")
    )
    monkeypatch.setattr(doc_agent_mod, "checkout_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(doc_agent_mod, "delete_branch", lambda *a, **kw: (True, ""))

    a = DocumentationAgent()
    out = a.run_full_workflow(
        repo_path=tmp_path,
        task_id="t1",
        task_summary="s",
        agent_type="backend",
        spec_content="",
        architecture=None,
        codebase_content="code",
    )
    assert "commit failed" in out.summary


def test_doc_agent_run_full_workflow_merge_fails(monkeypatch, tmp_path: Path) -> None:
    _patch_agent(
        monkeypatch,
        [
            {"readme_content": "x", "readme_changed": True, "summary": "ok"},
            {"contributors_content": "", "contributors_changed": False, "summary": "ok"},
        ],
    )
    monkeypatch.setattr(
        doc_agent_mod, "create_feature_branch", lambda *a, **kw: (True, "docs/t1")
    )
    monkeypatch.setattr(doc_agent_mod, "write_files_and_commit", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(
        doc_agent_mod, "merge_branch", lambda *a, **kw: (False, "conflict")
    )
    monkeypatch.setattr(doc_agent_mod, "checkout_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(doc_agent_mod, "delete_branch", lambda *a, **kw: (True, ""))

    a = DocumentationAgent()
    out = a.run_full_workflow(
        repo_path=tmp_path,
        task_id="t1",
        task_summary="s",
        agent_type="backend",
        spec_content="",
        architecture=None,
        codebase_content="code",
    )
    assert "merge failed" in out.summary


def test_doc_agent_run_full_workflow_exception(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        doc_agent_mod, "create_feature_branch", lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("git exploded")
        )
    )
    a = DocumentationAgent()
    out = a.run_full_workflow(
        repo_path=tmp_path,
        task_id="t1",
        task_summary="s",
        agent_type="backend",
        spec_content="",
        architecture=None,
        codebase_content="code",
    )
    assert "Documentation update failed" in out.summary


def test_doc_agent_run_full_workflow_timeout(monkeypatch, tmp_path: Path) -> None:
    """If the workflow exceeds MAX_WORKFLOW_SECONDS, it bails out cleanly."""
    monkeypatch.setattr(
        doc_agent_mod, "create_feature_branch", lambda *a, **kw: (True, "docs/t1")
    )
    monkeypatch.setattr(doc_agent_mod, "checkout_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(doc_agent_mod, "delete_branch", lambda *a, **kw: (True, ""))

    # Force timeout immediately
    monkeypatch.setattr(doc_agent_mod, "MAX_WORKFLOW_SECONDS", -1)

    a = DocumentationAgent()
    out = a.run_full_workflow(
        repo_path=tmp_path,
        task_id="t1",
        task_summary="s",
        agent_type="backend",
        spec_content="",
        architecture=None,
        codebase_content="code",
    )
    assert "timeout" in out.summary.lower()


def test_doc_agent_read_file(tmp_path: Path) -> None:
    f = tmp_path / "README.md"
    f.write_text("hi", encoding="utf-8")
    assert DocumentationAgent._read_file(f) == "hi"
    assert DocumentationAgent._read_file(tmp_path / "missing.md") == ""


def test_doc_agent_read_file_oserror(monkeypatch, tmp_path: Path) -> None:
    f = tmp_path / "fail.md"
    f.write_text("x", encoding="utf-8")

    real_exists = Path.exists

    def _exists(self):
        if self == f:
            raise OSError("disk error")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _exists)
    assert DocumentationAgent._read_file(f) == ""


def test_doc_agent_cleanup_branch_swallows_errors(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        doc_agent_mod, "checkout_branch", lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("nope")
        )
    )
    monkeypatch.setattr(doc_agent_mod, "delete_branch", lambda *a, **kw: (True, ""))
    a = DocumentationAgent()
    # Should not raise
    a._cleanup_branch(tmp_path, "docs/x")


def test_doc_agent_run_final_review_uses_extensions(monkeypatch, tmp_path: Path) -> None:
    """run_final_review calls _read_repo_code with backend or frontend extensions."""
    captured = {}

    def _fake_read_repo_code(path, exts):
        captured["exts"] = exts
        return "code"

    monkeypatch.setattr(doc_agent_mod, "_read_repo_code", _fake_read_repo_code)
    # Stub the rest so the workflow is a no-op
    monkeypatch.setattr(
        doc_agent_mod, "create_feature_branch", lambda *a, **kw: (True, "docs/f")
    )
    monkeypatch.setattr(doc_agent_mod, "checkout_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(doc_agent_mod, "delete_branch", lambda *a, **kw: (True, ""))
    _patch_agent(
        monkeypatch,
        [
            {"readme_content": "", "readme_changed": False, "summary": "ok"},
            {"contributors_content": "", "contributors_changed": False, "summary": "ok"},
        ],
    )

    a = DocumentationAgent()
    out = a.run_final_review(
        repo_path=tmp_path,
        repo_name="backend",
        spec_content="spec",
        architecture=None,
        completed_task_ids=["t1", "t2"],
    )
    assert isinstance(out, DocumentationOutput)
    assert captured["exts"] == [".py"]

    # And frontend chooses the other extension list
    a.run_final_review(
        repo_path=tmp_path,
        repo_name="frontend",
        spec_content="spec",
        architecture=None,
        completed_task_ids=[],
    )
    assert ".ts" in captured["exts"]


def test_doc_agent_run_final_review_read_codebase_fails(monkeypatch, tmp_path: Path) -> None:
    def _boom(path, exts):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(doc_agent_mod, "_read_repo_code", _boom)
    a = DocumentationAgent()
    out = a.run_final_review(
        repo_path=tmp_path,
        repo_name="backend",
        spec_content="",
        architecture=None,
        completed_task_ids=[],
    )
    assert "Final review skipped" in out.summary
