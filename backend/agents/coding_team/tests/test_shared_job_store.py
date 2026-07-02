"""Unit tests for shared_job_store (team-agnostic read + HITL operations)."""

from __future__ import annotations

import shared_job_store


class _FakeClient:
    def __init__(self, job=None):
        self.job = job
        self.updates: list = []

    def get_job(self, job_id):
        return self.job

    def atomic_update(self, job_id, *, merge_fields=None, append_to=None):
        self.updates.append({"merge": merge_fields, "append": append_to})


def test_get_job_passes_through() -> None:
    assert shared_job_store.get_job(_FakeClient(job={"id": "j"}), "j") == {"id": "j"}


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
