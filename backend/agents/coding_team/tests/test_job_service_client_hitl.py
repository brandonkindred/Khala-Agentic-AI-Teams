"""Unit tests for job_service_client's shared HITL pause/answer operations."""

from __future__ import annotations

import job_service_client as shared_job_store


class _FakeClient:
    def __init__(self, job=None):
        self.job = job
        self.updates: list = []

    def get_job(self, job_id):
        return self.job

    def atomic_update(self, job_id, *, merge_fields=None, append_to=None):
        self.updates.append({"merge": merge_fields, "append": append_to})


def test_add_pending_questions_sets_waiting_and_appends() -> None:
    client = _FakeClient()
    shared_job_store.add_pending_questions(client, "j", [{"q": "1"}])
    assert client.updates == [
        {"merge": {"waiting_for_answers": True}, "append": {"pending_questions": [{"q": "1"}]}}
    ]


def test_submit_answers_clears_and_appends() -> None:
    client = _FakeClient()
    shared_job_store.submit_answers(client, "j", [{"a": "x"}])
    assert client.updates == [
        {
            "merge": {"pending_questions": [], "waiting_for_answers": False},
            "append": {"submitted_answers": [{"a": "x"}]},
        }
    ]


def test_is_waiting_for_answers() -> None:
    assert shared_job_store.is_waiting_for_answers(
        _FakeClient(job={"waiting_for_answers": True}), "j"
    )
    assert not shared_job_store.is_waiting_for_answers(_FakeClient(job={}), "j")
    assert not shared_job_store.is_waiting_for_answers(_FakeClient(job=None), "j")


def test_get_submitted_answers_coerces_none_and_missing() -> None:
    # The reconciled behaviour: a stored None becomes [].
    assert (
        shared_job_store.get_submitted_answers(_FakeClient(job={"submitted_answers": None}), "j")
        == []
    )
    assert shared_job_store.get_submitted_answers(
        _FakeClient(job={"submitted_answers": [1, 2]}), "j"
    ) == [
        1,
        2,
    ]
    assert shared_job_store.get_submitted_answers(_FakeClient(job=None), "j") == []


def test_make_cachedir_hitl_binds_cachedir_wrappers() -> None:
    """The factory both team job-stores use returns four cache_dir-keyed wrappers
    that delegate to the client the getter yields, with public wrapper names."""
    seen: dict = {}

    def _getter(cache_dir):
        seen["cache_dir"] = cache_dir
        return _FakeClient(job={"waiting_for_answers": True, "submitted_answers": [7]})

    add, submit, waiting, get = shared_job_store.make_cachedir_hitl(_getter, "/default/cache")

    # __name__ preserved for clean tracebacks/introspection.
    assert (add.__name__, submit.__name__, waiting.__name__, get.__name__) == (
        "add_pending_questions",
        "submit_answers",
        "is_waiting_for_answers",
        "get_submitted_answers",
    )
    # Default cache_dir flows to the getter; delegation reaches the client.
    assert waiting("j") is True
    assert seen["cache_dir"] == "/default/cache"
    assert get("j") == [7]
    # An explicit cache_dir overrides the default.
    waiting("j", cache_dir="/other")
    assert seen["cache_dir"] == "/other"
