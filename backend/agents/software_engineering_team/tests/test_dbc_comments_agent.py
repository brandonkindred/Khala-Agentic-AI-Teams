"""Unit tests for DbcCommentsAgent.

``DbcCommentsAgent`` calls the injected ``LLMClient``'s ``complete_json``
directly (via ``llm_service.complete_validated``, validated against
``DbcCommentsLLMResponse``) -- no Strands ``Agent``/``Model`` is built for
this call path. Uses ``DummyLLMClient`` subclasses instead of a Strands
model double so the injected client behaves like a real ``LLMClient``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.technical_writers.dbc_comments_agent import agent as dbc_mod
from software_engineering_team.technical_writers.dbc_comments_agent.agent import (
    DbcCommentsAgent,
)
from software_engineering_team.technical_writers.dbc_comments_agent.models import (
    DbcCommentsInput,
    DbcCommentsStatus,
)


class _StubClient(DummyLLMClient):
    """DummyLLMClient subclass returning a canned DbcCommentsLLMResponse-shaped
    dict, or always raising, on every call."""

    def __init__(
        self, canned: Optional[Any] = None, raise_exc: Optional[BaseException] = None
    ) -> None:
        super().__init__()
        self._canned = canned
        self._raise = raise_exc
        self.calls: List[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        self.calls.append(prompt)
        if self._raise is not None:
            raise self._raise
        return self._canned if self._canned is not None else {}


class _SequencedClient(DummyLLMClient):
    """DummyLLMClient subclass returning/raising a different scripted response
    per call, in order -- used to prove a retry can actually recover."""

    def __init__(self, responses: List[Union[BaseException, Dict[str, Any]]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self.calls: List[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Any:
        self.calls.append(prompt)
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _agent(llm: DummyLLMClient) -> DbcCommentsAgent:
    return DbcCommentsAgent(llm_client=llm)


def test_dbc_init_resolves_default_client_via_get_client(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(dbc_mod, "get_client", lambda key: sentinel)
    a = DbcCommentsAgent()
    assert a.llm is sentinel


def test_dbc_init_accepts_injected_llm_client() -> None:
    client = DummyLLMClient()
    a = DbcCommentsAgent(llm_client=client)
    assert a.llm is client


def test_dbc_run_empty_code_returns_compliant() -> None:
    client = _StubClient()
    a = _agent(client)
    out = a.run(DbcCommentsInput(code="   "))
    assert out.already_compliant is True
    assert "No code" in out.summary
    assert client.calls == []  # never called the LLM


def test_dbc_run_already_compliant() -> None:
    client = _StubClient(
        canned={
            "insertions": [],
            "already_compliant": True,
            "summary": "perfectly compliant",
        }
    )
    a = _agent(client)
    out = a.run(
        DbcCommentsInput(
            code="def x(): pass",
            language="python",
            task_description="check it",
        )
    )
    assert out.already_compliant is True
    assert "perfectly" in out.summary


def test_dbc_run_with_insertions_returned() -> None:
    """comments_added/comments_updated and files are computed by the real,
    deterministic merge (merge.apply_dbc_insertions) -- never trusted from the
    LLM's self-reported counts, which is why the fixture's counts (3/1) are
    intentionally wrong and must not appear in the result."""
    client = _StubClient(
        canned={
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
    a = _agent(client)
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


def test_dbc_run_rejects_invalid_insertion_without_corrupting() -> None:
    """An insertion the merge cannot safely anchor is surfaced via
    rejected_insertions and simply omitted from files -- never corrupted."""
    client = _StubClient(
        canned={
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
    a = _agent(client)
    out = a.run(DbcCommentsInput(code="def f():\n    pass\n", language="python"))
    assert out.already_compliant is False
    assert len(out.insertions) == 1  # still visible for observability
    assert out.files == {}
    assert out.comments_added == 0
    assert out.comments_updated == 0
    assert len(out.rejected_insertions) == 1
    assert "does_not_exist" in out.rejected_insertions[0]


def test_dbc_run_merge_exception_fails_loud(monkeypatch) -> None:
    """An unexpected exception from the deterministic merge step must not
    propagate out of run() -- it is caught and surfaced as a failed,
    non-compliant result (like an exhausted LLM-call failure), honoring
    run()'s documented 'Raises: Nothing' contract without silently claiming
    compliance. merge.py is pure/LLM-free, so there is nothing for a retry
    to fix here -- reaching this path means an unexpected bug, not a
    transient condition."""
    client = _StubClient(
        canned={
            "insertions": [{"file": "a.py", "symbol": "f", "comment": "c"}],
            "already_compliant": False,
            "summary": "added comments",
        }
    )
    a = _agent(client)

    def _boom(code, insertions):
        raise RuntimeError("merge blew up")

    monkeypatch.setattr(dbc_mod, "apply_dbc_insertions", _boom)
    statuses = []
    out = a.run(
        DbcCommentsInput(code="def f():\n    pass\n", language="python"),
        on_status=lambda s, d: statuses.append((s, d)),
    )
    assert out.already_compliant is False
    assert "merge error" in out.summary
    assert any(s == DbcCommentsStatus.FAILED for s, _ in statuses)


def test_dbc_run_persistent_llm_failure_surfaces_as_non_compliant_needs_retry() -> None:
    """A persistent LLM failure must retry at least once via complete_validated,
    then surface as already_compliant=False with NEEDS_RETRY/FAILED status
    callbacks -- never silently marking the code compliant."""
    client = _StubClient(raise_exc=RuntimeError("boom"))
    a = _agent(client)
    statuses = []
    out = a.run(
        DbcCommentsInput(code="def f(): pass"),
        on_status=lambda s, d: statuses.append(s),
    )
    assert out.already_compliant is False
    assert "failed" in out.summary.lower()
    assert statuses.count(DbcCommentsStatus.NEEDS_RETRY) >= 1
    assert DbcCommentsStatus.FAILED in statuses
    assert statuses.index(DbcCommentsStatus.NEEDS_RETRY) < statuses.index(DbcCommentsStatus.FAILED)
    assert len(client.calls) == dbc_mod._MAX_LLM_ATTEMPTS  # proves the retry actually fired


def test_dbc_run_recovers_after_one_transient_failure() -> None:
    """A transient failure on attempt 1 that succeeds on attempt 2 must return
    the successful result, not fail -- the retry has teeth."""
    good_payload = {"insertions": [], "already_compliant": True, "summary": "ok"}
    client = _SequencedClient([RuntimeError("transient"), good_payload])
    out = _agent(client).run(DbcCommentsInput(code="def f(): pass"))
    assert out.already_compliant is True
    assert len(client.calls) == 2


def test_dbc_run_non_dict_top_level_json_fails_loud_non_compliant() -> None:
    """A reply that isn't a JSON object at all fails schema validation on
    every attempt, exhausting the retry -- surfaced as non-compliant, never
    silently marked compliant."""
    client = _StubClient(canned=[])  # a list, not an object -- fails schema validation
    out = _agent(client).run(DbcCommentsInput(code="def f(): pass"))
    assert out.already_compliant is False
    assert "failed" in out.summary.lower()


def test_dbc_run_non_list_insertions() -> None:
    client = _StubClient(canned={"insertions": "not a list", "already_compliant": False})
    out = _agent(client).run(DbcCommentsInput(code="def f(): pass"))
    assert out.already_compliant is False
    assert out.insertions == []


def test_dbc_run_malformed_insertion_entry_fails_loud() -> None:
    """One malformed insertion entry (missing a required field) fails schema
    validation of the WHOLE response, not just that entry -- unlike the old
    per-item tolerance, a persistently malformed reply now drives the retry
    and then the fail-loud path, never a silent partial acceptance."""
    client = _StubClient(
        canned={
            "insertions": [
                {"file": "good.py", "symbol": "f", "comment": "docstring"},
                {"file": "missing_comment.py", "symbol": "g"},  # missing required "comment"
            ],
            "already_compliant": False,
            "summary": "ok",
        }
    )
    out = _agent(client).run(DbcCommentsInput(code="def f(): pass"))
    assert out.already_compliant is False


def test_dbc_run_safety_override() -> None:
    """LLM says not compliant but returned no insertions -> override to compliant."""
    client = _StubClient(canned={"insertions": [], "already_compliant": False, "summary": ""})
    out = _agent(client).run(DbcCommentsInput(code="def f(): pass"))
    assert out.already_compliant is True
    assert "No changes needed" in out.summary


def test_dbc_run_compliant_no_summary_default_praise() -> None:
    client = _StubClient(
        canned={
            "insertions": [],
            "already_compliant": True,
            "summary": "",
        }
    )
    out = _agent(client).run(DbcCommentsInput(code="def f(): pass"))
    assert "Excellent" in out.summary


def test_dbc_run_with_architecture_context() -> None:
    from shared.dev_models.models import SystemArchitecture

    client = _StubClient(canned={"insertions": [], "already_compliant": True, "summary": "ok"})
    arch = SystemArchitecture(overview="big picture")
    out = _agent(client).run(
        DbcCommentsInput(
            code="def f(): pass",
            task_description="task",
            architecture=arch,
        )
    )
    # Verify the prompt was built with the architecture info
    assert any("big picture" in c for c in client.calls)
    assert out.already_compliant


def test_dbc_status_callbacks_fire() -> None:
    client = _StubClient(canned={"insertions": [], "already_compliant": True, "summary": "ok"})
    seen = []
    _agent(client).run(
        DbcCommentsInput(code="def f(): pass"),
        on_status=lambda s, d: seen.append(s),
    )
    assert DbcCommentsStatus.STARTING in seen
    assert DbcCommentsStatus.COMPLETE in seen


def test_dbc_run_survives_misbehaving_on_status_callback() -> None:
    """A raising on_status callback must not propagate out of run() -- it is
    an observability hook, not part of the review's control flow, so run()'s
    documented 'Raises: Nothing' contract holds even when the caller's own
    callback is broken."""
    client = _StubClient(canned={"insertions": [], "already_compliant": True, "summary": "ok"})

    def _boom(status, detail):
        raise RuntimeError("callback exploded")

    out = _agent(client).run(DbcCommentsInput(code="def f(): pass"), on_status=_boom)
    assert out.already_compliant is True
