"""Unit tests for DbcCommentsAgent."""

from __future__ import annotations

import json

from software_engineering_team.shared import llm as llm_mod
from software_engineering_team.technical_writers.dbc_comments_agent import agent as dbc_mod
from software_engineering_team.technical_writers.dbc_comments_agent.agent import (
    DbcCommentsAgent,
)
from software_engineering_team.technical_writers.dbc_comments_agent.models import (
    DbcCommentsInput,
    DbcCommentsStatus,
)
from software_engineering_team.tests.conftest import _strands_model_double


class _FakeCompleteJson:
    """Stand-in for complete_json_with_continuation used to unit-test DbcCommentsAgent
    without exercising the real parsing/recovery logic (that's covered separately in
    test_shared_llm.py)."""

    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc
        self.calls = []

    def __call__(self, model, prompt, *, system_prompt=None, **kwargs):
        self.calls.append(prompt)
        if self._raise:
            raise self._raise
        return self._payload or {}


def _build_agent(monkeypatch, fake):
    monkeypatch.setattr(dbc_mod, "complete_json_with_continuation", fake)
    monkeypatch.setattr(dbc_mod, "get_strands_model", lambda _k=None, **_kw: object())
    return DbcCommentsAgent()


def test_dbc_init_uses_strands_model(monkeypatch) -> None:
    monkeypatch.setattr(dbc_mod, "get_strands_model", lambda key, **_kw: object())
    a = DbcCommentsAgent()
    assert a._model is not None


def test_dbc_init_accepts_strands_model_instance(monkeypatch) -> None:
    m = _strands_model_double()
    a = DbcCommentsAgent(llm_client=m)
    assert a._model is m


def test_dbc_run_empty_code_returns_compliant(monkeypatch) -> None:
    fake = _FakeCompleteJson()
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="   "))
    assert out.already_compliant is True
    assert "No code" in out.summary
    assert fake.calls == []  # never called the LLM


def test_dbc_run_already_compliant(monkeypatch) -> None:
    fake = _FakeCompleteJson(
        {
            "insertions": [],
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


def test_dbc_run_with_insertions_returned(monkeypatch) -> None:
    """comments_added/comments_updated and files are computed by the real,
    deterministic merge (merge.apply_dbc_insertions) -- never trusted from the
    LLM's self-reported counts, which is why the fixture's counts (3/1) are
    intentionally wrong and must not appear in the result."""
    fake = _FakeCompleteJson(
        {
            "insertions": [
                {
                    "file": "a.py",
                    "symbol": "f",
                    "line": 2,
                    "comment": '"""Does nothing.\n\nPostconditions:\n    - Returns None.\n"""',
                    "action": "add",
                }
            ],
            "already_compliant": False,
            "comments_added": 3,
            "comments_updated": 1,
            "summary": "added comments",
            "suggested_commit_message": "docs(dbc): comments",
        }
    )
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="def f():\n    pass\n", language="python"))
    assert out.already_compliant is False
    assert len(out.insertions) == 1
    assert out.insertions[0].file == "a.py"
    assert out.insertions[0].symbol == "f"
    assert out.insertions[0].action == "add"
    assert out.comments_added == 1
    assert out.comments_updated == 0
    assert out.rejected_insertions == []
    assert "Does nothing." in out.files["a.py"]
    assert "pass" in out.files["a.py"]
    assert out.suggested_commit_message == "docs(dbc): comments"


def test_dbc_run_rejects_invalid_insertion_without_corrupting(monkeypatch) -> None:
    """An insertion the merge cannot safely anchor is surfaced via
    rejected_insertions and simply omitted from files -- never corrupted."""
    fake = _FakeCompleteJson(
        {
            "insertions": [
                {
                    "file": "a.py",
                    "symbol": "does_not_exist",
                    "comment": "Never anchored.",
                    "action": "add",
                }
            ],
            "already_compliant": False,
            "summary": "added comments",
        }
    )
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="def f():\n    pass\n", language="python"))
    assert out.already_compliant is False
    assert len(out.insertions) == 1  # still visible for observability
    assert out.files == {}
    assert out.comments_added == 0
    assert out.comments_updated == 0
    assert len(out.rejected_insertions) == 1
    assert "does_not_exist" in out.rejected_insertions[0]


def test_dbc_run_llm_exception_fails_open(monkeypatch) -> None:
    fake = _FakeCompleteJson(raise_exc=RuntimeError("oops"))
    a = _build_agent(monkeypatch, fake)
    statuses = []
    out = a.run(
        DbcCommentsInput(code="def f(): pass"),
        on_status=lambda s, d: statuses.append((s, d)),
    )
    assert out.already_compliant is True
    assert "DbC review skipped" in out.summary
    assert any(s == DbcCommentsStatus.FAILED for s, _ in statuses)


def test_dbc_run_non_dict_top_level_json_fails_open(monkeypatch) -> None:
    """A recovered-but-non-object top-level JSON value (e.g. a fenced `[]`) must
    take the fail-open path, not crash with AttributeError on data.get(...)."""

    def _returns_list(model, prompt, *, system_prompt=None, **kwargs):
        return []

    a = _build_agent(monkeypatch, _returns_list)
    out = a.run(DbcCommentsInput(code="def f(): pass"))
    assert out.already_compliant is True
    assert "DbC review skipped" in out.summary


def test_dbc_run_non_list_insertions(monkeypatch) -> None:
    fake = _FakeCompleteJson({"insertions": "not a list", "already_compliant": False})
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="def f(): pass"))
    # Falls back to compliant since no actionable insertions
    assert out.already_compliant is True
    assert out.insertions == []


def test_dbc_run_filters_invalid_insertion_entries(monkeypatch) -> None:
    """Verifies malformed insertion entries (missing required fields, wrong
    type) are skipped rather than failing the whole review."""
    fake = _FakeCompleteJson(
        {
            "insertions": [
                {"file": "good.py", "symbol": "f", "comment": "docstring"},
                {"file": "missing_comment.py", "symbol": "g"},
                "not a dict",
            ],
            "already_compliant": False,
            "summary": "ok",
        }
    )
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="def f(): pass"))
    assert len(out.insertions) == 1
    assert out.insertions[0].file == "good.py"


def test_dbc_run_safety_override(monkeypatch) -> None:
    """LLM says not compliant but returned no insertions -> override to compliant."""
    fake = _FakeCompleteJson({"insertions": [], "already_compliant": False, "summary": ""})
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="def f(): pass"))
    assert out.already_compliant is True
    assert "No changes needed" in out.summary


def test_dbc_run_compliant_no_summary_default_praise(monkeypatch) -> None:
    fake = _FakeCompleteJson(
        {
            "insertions": [],
            "already_compliant": True,
            "summary": "",
        }
    )
    a = _build_agent(monkeypatch, fake)
    out = a.run(DbcCommentsInput(code="def f(): pass"))
    assert "Excellent" in out.summary


def test_dbc_run_with_architecture_context(monkeypatch) -> None:
    from software_engineering_team.shared.models import SystemArchitecture

    fake = _FakeCompleteJson({"insertions": [], "already_compliant": True, "summary": "ok"})
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
    fake = _FakeCompleteJson({"insertions": [], "already_compliant": True, "summary": "ok"})
    a = _build_agent(monkeypatch, fake)
    seen = []
    a.run(
        DbcCommentsInput(code="def f(): pass"),
        on_status=lambda s, d: seen.append(s),
    )
    assert DbcCommentsStatus.STARTING in seen
    assert DbcCommentsStatus.COMPLETE in seen


def test_dbc_run_recovers_fenced_json_response(monkeypatch) -> None:
    """End-to-end (no complete_json_with_continuation mocking): a markdown-fenced
    LLM response is recovered instead of raising, exercising the real
    extract_json_from_response fallback through the shared helper."""

    class _FencedAgent:
        def __call__(self, prompt, **kwargs):
            payload = {"insertions": [], "already_compliant": True, "summary": "fenced ok"}
            return "```json\n" + json.dumps(payload) + "\n```"

    monkeypatch.setattr(llm_mod, "Agent", lambda *a, **kw: _FencedAgent())
    a = DbcCommentsAgent(llm_client=_strands_model_double())
    out = a.run(DbcCommentsInput(code="def f(): pass"))
    assert out.already_compliant is True
    assert "fenced" in out.summary
