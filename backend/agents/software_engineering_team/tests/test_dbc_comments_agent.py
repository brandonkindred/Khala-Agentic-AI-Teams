"""Unit tests for DbcCommentsAgent."""

from __future__ import annotations

import json

from software_engineering_team.technical_writers.dbc_comments_agent import agent as dbc_mod
from software_engineering_team.technical_writers.dbc_comments_agent.agent import (
    DbcCommentsAgent,
)
from software_engineering_team.technical_writers.dbc_comments_agent.models import (
    DbcCommentsInput,
    DbcCommentsStatus,
)


class _FakeAgent:
    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        if self._raise:
            raise self._raise
        return json.dumps(self._payload or {})


def _build_agent(monkeypatch, fake):
    monkeypatch.setattr(dbc_mod, "Agent", lambda *a, **kw: fake)
    monkeypatch.setattr(dbc_mod, "get_strands_model", lambda _k=None, **_kw: object())
    return DbcCommentsAgent()


def test_dbc_init_uses_strands_model(monkeypatch) -> None:
    monkeypatch.setattr(dbc_mod, "get_strands_model", lambda key, **_kw: object())
    monkeypatch.setattr(dbc_mod, "Agent", lambda *a, **kw: object())
    a = DbcCommentsAgent()
    assert a._agent is not None


def test_dbc_init_accepts_strands_model_instance(monkeypatch) -> None:
    from strands.models.model import Model as StrandsModel

    class _M(StrandsModel):
        def __init__(self):
            pass

        def update_config(self, *a, **kw):
            pass

        def get_config(self):
            return {}

        def structured_output(self, *a, **kw):  # pragma: no cover
            return {}

        async def stream(self, *a, **kw):  # pragma: no cover
            yield {}

    monkeypatch.setattr(dbc_mod, "Agent", lambda *a, **kw: "agent")
    a = DbcCommentsAgent(llm_client=_M())
    assert a._agent == "agent"


def test_dbc_run_empty_code_returns_compliant(monkeypatch) -> None:
    fake = _FakeAgent()
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="   "))
    assert out.already_compliant is True
    assert "No code" in out.summary
    assert fake.calls == []  # never called the LLM


def test_dbc_run_already_compliant(monkeypatch) -> None:
    fake = _FakeAgent(
        {
            "files": {},
            "already_compliant": True,
            "summary": "perfectly compliant",
        }
    )
    a = _build_agent(monkeypatch, fake)
    out = a.run(
        DbcCommentsInput(
            code="def x(): pass",
            language="python",
            task_description="check it",
        )
    )
    assert out.already_compliant is True
    assert "perfectly" in out.summary


def test_dbc_run_with_files_changed(monkeypatch) -> None:
    fake = _FakeAgent(
        {
            "files": {"a.py": "# x\ndef f():\n    pass\n"},
            "already_compliant": False,
            "comments_added": 3,
            "comments_updated": 1,
            "summary": "added comments",
            "suggested_commit_message": "docs(dbc): comments",
        }
    )
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="def f(): pass", language="python"))
    assert out.already_compliant is False
    assert "a.py" in out.files
    assert out.comments_added == 3
    assert out.comments_updated == 1
    assert out.suggested_commit_message == "docs(dbc): comments"


def test_dbc_run_llm_exception_fails_open(monkeypatch) -> None:
    fake = _FakeAgent(raise_exc=RuntimeError("oops"))
    a = _build_agent(monkeypatch, fake)
    statuses = []
    out = a.run(
        DbcCommentsInput(code="def f(): pass"),
        on_status=lambda s, d: statuses.append((s, d)),
    )
    assert out.already_compliant is True
    assert "DbC review skipped" in out.summary
    assert any(s == DbcCommentsStatus.FAILED for s, _ in statuses)


def test_dbc_run_non_dict_files(monkeypatch) -> None:
    fake = _FakeAgent({"files": "not a dict", "already_compliant": False})
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="def f(): pass"))
    # Falls back to compliant since no actionable files
    assert out.already_compliant is True
    assert out.files == {}


def test_dbc_run_filters_invalid_file_entries(monkeypatch) -> None:
    """Bypasses JSON serialization to verify the post-parse filter directly."""

    class _RawFake:
        def __call__(self, prompt):
            # Return a raw JSON string that the agent will json.loads(); then
            # the filter strips empty content. We can't put non-string keys in
            # JSON so we rely on empty-content filtering and non-string values.
            return (
                '{"files": {"good.py": "code", "empty.py": "   ", "bad.py": 123},'
                ' "already_compliant": false, "summary": "ok"}'
            )

    fake = _RawFake()
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="def f(): pass"))
    assert set(out.files.keys()) == {"good.py"}


def test_dbc_run_safety_override(monkeypatch) -> None:
    """LLM says not compliant but returned no files -> override to compliant."""
    fake = _FakeAgent({"files": {}, "already_compliant": False, "summary": ""})
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="def f(): pass"))
    assert out.already_compliant is True
    assert "No changes needed" in out.summary


def test_dbc_run_compliant_no_summary_default_praise(monkeypatch) -> None:
    fake = _FakeAgent(
        {
            "files": {},
            "already_compliant": True,
            "summary": "",
        }
    )
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="def f(): pass"))
    assert "Excellent" in out.summary


def test_dbc_run_with_architecture_context(monkeypatch) -> None:
    from software_engineering_team.shared.models import SystemArchitecture

    fake = _FakeAgent({"files": {}, "already_compliant": True, "summary": "ok"})
    a = _build_agent(monkeypatch, fake)
    arch = SystemArchitecture(overview="big picture")
    out = a.run(
        DbcCommentsInput(
            code="def f(): pass",
            task_description="task",
            architecture=arch,
        )
    )
    # Verify the prompt was built with the architecture info
    assert any("big picture" in c for c in fake.calls)
    assert out.already_compliant


def test_dbc_status_callbacks_fire(monkeypatch) -> None:
    fake = _FakeAgent({"files": {}, "already_compliant": True, "summary": "ok"})
    a = _build_agent(monkeypatch, fake)
    seen = []
    a.run(
        DbcCommentsInput(code="def f(): pass"),
        on_status=lambda s, d: seen.append(s),
    )
    assert DbcCommentsStatus.STARTING in seen
    assert DbcCommentsStatus.COMPLETE in seen
